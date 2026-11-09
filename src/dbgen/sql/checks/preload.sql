-- Pre-load checks, run by `dbgen stage generate` on its tables before they are written. See
-- postload.sql for the checks `dbgen stage verify` runs after `load`.
--
-- Each query is named by a `-- name:` line and must return no rows. They come from the metadata
-- files, which compile writes as the tables meta_<name> (../../metadata.py), not from SQL written
-- per package: value types, option codes, compulsory data elements, mandatory and unique
-- attributes, repeatable stages, the org units of programs. The program rules are the spec's to
-- satisfy, which `dbgen validate` checks.
--
-- Inputs:
-- * ../tables.sql: the DHIS2 tables, metadata by UID
-- * ../../generate.py: event_value, the data values of every event; orgunit, user_ref; the
--   meta_* tables; and the variables start_date, end_date and now_ts

-- name: data element not in its program stage
select ev.eventid, v.de_uid, ev.programstage_uid
from event_value v
join event ev using (eventid)
left join meta_programstagedataelement psde
  on psde.programstage_uid = ev.programstage_uid and psde.dataelement_uid = v.de_uid
where psde.dataelement_uid is null;

-- name: value does not parse as its value type
select v.eventid, v.de_uid, de.valuetype, v.value
from event_value v
join meta_dataelement de on de.uid = v.de_uid
where de.optionset_uid is null and not valid_value(de.valuetype, v.value);

-- name: option code not in the data element's option set
select v.eventid, v.de_uid, v.value
from event_value v
join meta_dataelement de on de.uid = v.de_uid
left join meta_option o on o.optionset_uid = de.optionset_uid and o.code = v.value
where de.optionset_uid is not null and o.code is null;

-- name: option code that does not parse as the data element's value type
select v.eventid, v.de_uid, de.valuetype, v.value
from event_value v
join meta_dataelement de on de.uid = v.de_uid
where de.optionset_uid is not null and not valid_value(de.valuetype, v.value);

-- name: compulsory data element missing on a completed event
with compulsory as (
    select programstage_uid, list(dataelement_uid) as de_uids
    from meta_programstagedataelement
    where compulsory
    group by all
),
missing as (
    select ev.eventid, ev.programstage_uid,
           list_filter(c.de_uids, lambda d: not list_contains(
               list_transform(ev.datavalues, lambda x: x.k), d)) as de_uids
    from event ev
    join compulsory c using (programstage_uid)
    where ev.status = 'COMPLETED'
)
select eventid, programstage_uid, unnest(de_uids) as de_uid
from missing
where len(de_uids) > 0;

-- name: values on an event that is scheduled or skipped
select eventid, status
from event
where status in ('SCHEDULE', 'SKIPPED') and len(datavalues) > 0;

-- name: occurred date that does not fit the status
select eventid, status, occurreddate
from event
where (occurreddate is null) <> (status in ('SCHEDULE', 'SKIPPED')) or scheduleddate is null;

-- name: more than one event of a non-repeatable stage per enrollment
select ev.enrollmentid, ev.programstage_uid, count(*) as events
from event ev
join meta_programstage ps on ps.uid = ev.programstage_uid
where not ps.repeatable
group by all
having count(*) > 1;

-- name: enrollment at an org unit its program is not assigned to
-- generate draws records at the org units of the build's area only.
select e.enrollmentid, e.organisationunit_uid
from enrollment e
join meta_program p on p.uid = e.program_uid
left join orgunit o
  on o.uid = e.organisationunit_uid and o.draws_records_and_users and o.level_name = p.orgunit_level
where o.uid is null;

-- name: tracked entity type differs from the program's
select e.enrollmentid
from enrollment e
join trackedentity te using (trackedentityid)
join meta_program p on p.uid = e.program_uid
where te.trackedentitytype_uid <> p.trackedentitytype_uid;

-- name: event stage belongs to another program than its enrollment
select ev.eventid
from event ev
join enrollment e using (enrollmentid)
join meta_programstage ps on ps.uid = ev.programstage_uid
where ps.program_uid <> e.program_uid;

-- name: mandatory program attribute missing
select distinct e.trackedentityid, pa.trackedentityattribute_uid
from enrollment e
join meta_program_attribute pa on pa.program_uid = e.program_uid and pa.mandatory
left join trackedentityattributevalue av
  on av.trackedentityid = e.trackedentityid
 and av.trackedentityattribute_uid = pa.trackedentityattribute_uid
where av.value is null;

-- name: attribute neither of the tracked entity's programs nor of its type
select av.trackedentityid, av.trackedentityattribute_uid
from trackedentityattributevalue av
join trackedentity te using (trackedentityid)
where not exists (
        select 1 from enrollment e
        join meta_program_attribute pa on pa.program_uid = e.program_uid
        where e.trackedentityid = av.trackedentityid
          and pa.trackedentityattribute_uid = av.trackedentityattribute_uid)
  and not exists (
        select 1 from meta_trackedentitytypeattribute ta
        where ta.trackedentitytype_uid = te.trackedentitytype_uid
          and ta.trackedentityattribute_uid = av.trackedentityattribute_uid);

-- name: attribute value does not parse as its value type
select av.trackedentityid, tea.uid, tea.valuetype, av.value
from trackedentityattributevalue av
join meta_trackedentityattribute tea on tea.uid = av.trackedentityattribute_uid
where tea.optionset_uid is null and not valid_value(tea.valuetype, av.value);

-- name: attribute option code not in its option set
select av.trackedentityid, tea.uid, av.value
from trackedentityattributevalue av
join meta_trackedentityattribute tea on tea.uid = av.trackedentityattribute_uid
left join meta_option o on o.optionset_uid = tea.optionset_uid and o.code = av.value
where tea.optionset_uid is not null and o.code is null;

-- name: duplicate value of a unique attribute
-- DHIS2's importer compares unique values ignoring case.
select av.trackedentityattribute_uid, lower(av.value), count(*)
from trackedentityattributevalue av
join meta_trackedentityattribute tea on tea.uid = av.trackedentityattribute_uid
where tea.unique
group by all
having count(*) > 1;

-- name: event before its enrollment, or a date outside the window
select ev.eventid, ev.occurreddate, ev.scheduleddate, e.enrollmentdate
from event ev
join enrollment e using (enrollmentid)
where coalesce(ev.occurreddate, ev.scheduleddate) < e.enrollmentdate
   or ev.occurreddate >= getvariable('end_date')
   or e.enrollmentdate < getvariable('start_date')
   or e.enrollmentdate >= getvariable('end_date');

-- name: record created after now
select 'event' as kind, eventid as id from event where created > getvariable('now_ts')
union all
select 'enrollment', enrollmentid from enrollment where created > getvariable('now_ts')
union all
select 'trackedentity', trackedentityid from trackedentity where created > getvariable('now_ts');

-- name: record updated before it was created
select 'event' as kind, eventid as id from event where lastupdated < created
union all
select 'enrollment', enrollmentid from enrollment where lastupdated < created
union all
select 'trackedentity', trackedentityid from trackedentity where lastupdated < created;

-- name: more than one active enrollment per tracked entity and program
select trackedentityid, program_uid, count(*)
from enrollment
where status = 'ACTIVE' and not deleted
group by all
having count(*) > 1;

-- name: more than one enrollment in a program that enrolls once
select e.trackedentityid, e.program_uid, count(*)
from enrollment e
join meta_program p on p.uid = e.program_uid
where p.only_enroll_once
group by all
having count(*) > 1;

-- name: completed date without completed status, or the reverse
select 'enrollment' as kind, enrollmentid as id from enrollment
where (status = 'COMPLETED') <> (completeddate is not null)
union all
select 'event', eventid from event
where (status = 'COMPLETED') <> (completeddate is not null);

-- name: duplicate uid
select uid, count(*)
from (
    select uid from trackedentity
    union all select uid from enrollment
    union all select uid from event
    union all select uid from relationship
)
group by uid
having count(*) > 1;

-- name: relationship of a type the metadata does not ship
select r.relationshipid, r.relationshiptype_uid
from relationship r
left join meta_relationshiptype rt on rt.uid = r.relationshiptype_uid
where rt.uid is null;

-- name: relationship created before one of its tracked entities
select r.relationshipid, r.created
from relationship r
join relationshipitem item using (relationshipid)
join trackedentity te using (trackedentityid)
where r.created < te.created;

-- name: duplicate relationship key
select key, count(*)
from relationship
group by all
having count(*) > 1;

-- name: record lost by a join of the tables
select 'trackedentity' as tbl from out_trackedentity
having count(*) <> (select count(*) from trackedentity)
union all
select 'enrollment' from out_enrollment having count(*) <> (select count(*) from enrollment)
union all
select 'event' from out_event having count(*) <> (select count(*) from event)
union all
select 'trackedentityattributevalue' from out_attributevalue
having count(*) <> (select count(*) from trackedentityattributevalue)
union all
select 'relationship' from out_relationship
having count(*) <> (select count(*) from relationship);
