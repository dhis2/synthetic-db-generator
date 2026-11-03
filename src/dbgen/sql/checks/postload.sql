-- Post-load checks, run by `dbgen stage verify` against Postgres after `load`.
--
-- Each query is named by a `-- name:` line and must return no rows. Postgres has checked what it
-- can enforce: primary keys and unique constraints as `load` inserted the rows, foreign keys and
-- unique indexes when `load` added them back. These checks cover what only DHIS2's code enforces,
-- such as its tracker importer, which `load` bypasses.
--
-- The data values are read by key, with jsonb_object_keys and ->, not with jsonb_each: on
-- Postgres 16, jsonb_each in a lateral join keeps memory for every event until the query ends,
-- more than Postgres has for a region's events.

-- name: tracked entity with an enrollment but no program owner
select distinct en.trackedentityid, en.programid
from enrollment en
left join trackedentityprogramowner o
  on o.trackedentityid = en.trackedentityid and o.programid = en.programid
where o.trackedentityid is null;

-- name: more than one event of a non-repeatable stage per enrollment
select ev.enrollmentid, ev.programstageid, count(*)
from event ev
join programstage ps using (programstageid)
where not ps.repeatable and not ev.deleted
group by ev.enrollmentid, ev.programstageid
having count(*) > 1;

-- name: event stage belongs to another program than its enrollment
select ev.eventid
from event ev
join enrollment en using (enrollmentid)
join programstage ps using (programstageid)
where ps.programid <> en.programid;

-- name: data value of a data element not in the event's stage
select ev.eventid, v.key
from event ev
cross join lateral (
  select k as key, ev.eventdatavalues -> k as value from jsonb_object_keys(ev.eventdatavalues) k
) v
left join dataelement de on de.uid = v.key
left join programstagedataelement psde
  on psde.dataelementid = de.dataelementid and psde.programstageid = ev.programstageid
where psde.programstagedataelementid is null;

-- name: data value that does not parse as its value type
-- The full check is valid_value (../values.sql), which preload.sql runs on the same values. load
-- only quotes them into eventdatavalues, so this one guards that trip and lets other types pass.
select ev.eventid, de.uid, de.valuetype, v.value ->> 'value'
from event ev
cross join lateral (
  select k as key, ev.eventdatavalues -> k as value from jsonb_object_keys(ev.eventdatavalues) k
) v
join dataelement de on de.uid = v.key
where de.optionsetid is null and not case de.valuetype
    when 'BOOLEAN' then v.value ->> 'value' in ('true', 'false')
    when 'TRUE_ONLY' then v.value ->> 'value' = 'true'
    when 'DATE' then v.value ->> 'value' ~ '^\d{4}-\d{2}-\d{2}$'
    when 'AGE' then v.value ->> 'value' ~ '^\d{4}-\d{2}-\d{2}$'
    when 'INTEGER_POSITIVE' then v.value ->> 'value' ~ '^[1-9]\d*$'
    when 'INTEGER_ZERO_OR_POSITIVE' then v.value ->> 'value' ~ '^(0|[1-9]\d*)$'
    when 'INTEGER_NEGATIVE' then v.value ->> 'value' ~ '^-[1-9]\d*$'
    when 'INTEGER' then v.value ->> 'value' ~ '^(0|-?[1-9]\d*)$'
    when 'NUMBER' then v.value ->> 'value' ~ '^-?\d+(\.\d+)?$'
    when 'PERCENTAGE' then v.value ->> 'value' ~ '^\d+(\.\d+)?$'
    when 'UNIT_INTERVAL' then v.value ->> 'value' ~ '^\d+(\.\d+)?$'
    else true
end;

-- name: option code that does not exist
select ev.eventid, de.uid, v.value ->> 'value'
from event ev
cross join lateral (
  select k as key, ev.eventdatavalues -> k as value from jsonb_object_keys(ev.eventdatavalues) k
) v
join dataelement de on de.uid = v.key
left join optionvalue ov on ov.optionsetid = de.optionsetid and ov.code = v.value ->> 'value'
where de.optionsetid is not null and ov.optionvalueid is null;

-- name: data value without the fields DHIS2 writes
-- One jsonpath call per event tests all its values: taking each value out with -> costs more than
-- twice as much. The first offending data element is looked up only for an event that has one.
select ev.eventid,
       jsonb_path_query_first(ev.eventdatavalues, '$.keyvalue() ? (!(exists(@.value.value)
           && exists(@.value.created) && exists(@.value.lastUpdated)
           && exists(@.value.providedElsewhere) && exists(@.value.createdByUserInfo)
           && exists(@.value.lastUpdatedByUserInfo))).key') as key
from event ev
where jsonb_path_exists(ev.eventdatavalues, '$.* ? (!(exists(@.value) && exists(@.created)
    && exists(@.lastUpdated) && exists(@.providedElsewhere) && exists(@.createdByUserInfo)
    && exists(@.lastUpdatedByUserInfo)))');

-- name: event before its enrollment
select ev.eventid
from event ev
join enrollment en using (enrollmentid)
where ev.occurreddate < en.enrollmentdate;

-- name: more than one active enrollment per tracked entity and program
select trackedentityid, programid, count(*)
from enrollment
where status = 'ACTIVE' and not deleted
group by trackedentityid, programid
having count(*) > 1;

-- name: completed date without completed status, or the reverse
select 'enrollment' as kind, enrollmentid as id from enrollment
where (status = 'COMPLETED') <> (completeddate is not null)
union all
select 'event', eventid from event
where (status = 'COMPLETED') <> (completeddate is not null);

-- name: date in the future
select 'event' as kind, eventid as id from event
where occurreddate > now() or created > now() or completeddate > now()
union all
select 'enrollment', enrollmentid from enrollment
where enrollmentdate > now() or created > now();

-- name: tracked entity updated before one of its enrollments or events
-- DHIS2's importer updates a tracked entity whenever it writes one of its enrollments or events
-- (DefaultTrackerBundleService.postCommit), so neither is ever newer than it. A tracked entity
-- updated on its own, such as an attribute, is newer than both, which is fine.
select te.trackedentityid
from trackedentity te
join enrollment en using (trackedentityid)
where en.lastupdated > te.lastupdated
union
select te.trackedentityid
from event ev
join enrollment en using (enrollmentid)
join trackedentity te using (trackedentityid)
where ev.lastupdated > te.lastupdated;

-- name: duplicate value of a unique attribute
select v.trackedentityattributeid, lower(v.value), count(*)
from trackedentityattributevalue v
join trackedentityattribute a using (trackedentityattributeid)
where a.uniquefield
group by v.trackedentityattributeid, lower(v.value)
having count(*) > 1;

-- name: enrollment org unit not assigned to its program
select en.enrollmentid
from enrollment en
left join program_organisationunits po
  on po.programid = en.programid and po.organisationunitid = en.organisationunitid
where po.programid is null;

-- name: sequence behind the loaded ids
-- hibernate_sequence numbers the program owners, relationships and their items, and the metadata.
select 'trackedentityinstance_sequence' from trackedentity
having max(trackedentityid) > (select last_value from trackedentityinstance_sequence)
union all
select 'programinstance_sequence' from enrollment
having max(enrollmentid) > (select last_value from programinstance_sequence)
union all
select 'programstageinstance_sequence' from event
having max(eventid) > (select last_value from programstageinstance_sequence)
union all
select 'hibernate_sequence' from trackedentityprogramowner
having max(trackedentityprogramownerid) > (select last_value from hibernate_sequence)
union all
select 'hibernate_sequence' from relationship
having max(relationshipid) > (select last_value from hibernate_sequence)
union all
select 'hibernate_sequence' from relationshipitem
having max(relationshipitemid) > (select last_value from hibernate_sequence);

-- name: relationship key that does not match its type and tracked entities
select r.relationshipid
from relationship r
join relationshiptype rt using (relationshiptypeid)
join relationshipitem f on f.relationshipitemid = r.from_relationshipitemid
join relationshipitem t on t.relationshipitemid = r.to_relationshipitemid
join trackedentity fte on fte.trackedentityid = f.trackedentityid
join trackedentity tte on tte.trackedentityid = t.trackedentityid
where r.key <> rt.uid || '_' || fte.uid || '_' || tte.uid
   or r.inverted_key <> rt.uid || '_' || tte.uid || '_' || fte.uid;
