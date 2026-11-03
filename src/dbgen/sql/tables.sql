-- DHIS2's tracker tables of the records generate drew, with the column names of the database
-- but for the metadata, which they reference by UID in a column <name>_uid that load swaps for the
-- internal ID of bootstrap's ID maps, and the data values, which stay a list until load builds
-- eventdatavalues (../dhis2/load.py).
--
-- Inputs:
-- * ../generate.py: the records, out_trackedentity, out_enrollment, out_event,
--   out_attributevalue and out_relationship, and user_ref, the users by username
--
-- Outputs: the tables trackedentity, trackedentityattributevalue, enrollment,
-- trackedentityprogramowner, event, relationship and relationshipitem, and updated: the rows
-- updated after their creation, which load updates in the order of their update.
--
-- Integer ids follow creation time, so the load order is time order across all org units and
-- programs. A tracker UID is a bijection of its id (../uid.py).

create or replace table staged_trackedentity as
select g.*, row_number() over (order by g.created, g.te_key) as trackedentityid
from out_trackedentity g;

create or replace table staged_enrollment as
select g.*, row_number() over (order by g.created, g.en_key) as enrollmentid
from out_enrollment g;

create or replace table staged_event as
select g.* exclude (datavalues), row_number() over (order by g.created, g.ev_key) as eventid
from out_event g;

create or replace table trackedentity as
select
    t.trackedentityid,
    tracked_entity_uid(t.trackedentityid) as uid,
    t.created,
    t.lastupdated,
    false as inactive,
    t.deleted,
    timestamp '1970-01-01' as lastsynchronized,
    t.orgunit as organisationunit_uid,
    t.tracked_entity_type as trackedentitytype_uid,
    u.uid as createdby_uid,
    u.uid as lastupdatedby_uid,
    false as potentialduplicate
from staged_trackedentity t
join user_ref u on u.username = t.clerk
order by t.trackedentityid;

create or replace table trackedentityattributevalue as
select
    t.trackedentityid,
    a.attribute as trackedentityattribute_uid,
    a.created,
    a.lastupdated,
    a.value,
    a.clerk as storedby
from out_attributevalue a
join staged_trackedentity t using (te_key)
order by t.trackedentityid, a.attribute;

create or replace table enrollment as
select
    e.enrollmentid,
    enrollment_uid(e.enrollmentid) as uid,
    e.created,
    e.lastupdated,
    e.enrollment_date::timestamp as occurreddate,
    e.enrollment_date::timestamp as enrollmentdate,
    e.completed as completeddate,
    e.followup,
    e.completed_by as completedby,
    e.deleted,
    e.status,
    t.trackedentityid,
    e.program as program_uid,
    e.orgunit as organisationunit_uid,
    u.uid as createdby_uid,
    u.uid as lastupdatedby_uid
from staged_enrollment e
join staged_trackedentity t using (te_key)
join user_ref u on u.username = e.clerk
order by e.enrollmentid;

-- One owner per tracked entity and program: the org unit of its first enrollment in it.
create or replace table trackedentityprogramowner as
select
    row_number() over (order by t.trackedentityid, owner.program) as trackedentityprogramownerid,
    t.trackedentityid,
    owner.program as program_uid,
    owner.created,
    owner.created as lastupdated,
    owner.orgunit as organisationunit_uid,
    owner.clerk as createdby
from (
    select te_key, program, min(created) as created, arg_min(orgunit, created) as orgunit,
           arg_min(clerk, created) as clerk
    from staged_enrollment
    group by all
) owner
join staged_trackedentity t using (te_key)
order by trackedentityprogramownerid;

create or replace table event as
select
    ev.eventid,
    event_uid(ev.eventid) as uid,
    ev.created,
    ev.lastupdated,
    e.enrollmentid,
    ev.stage as programstage_uid,
    ev.aoc as attributeoptioncombo_uid,
    ev.deleted,
    ev.scheduled::timestamp as scheduleddate,
    ev.occurred::timestamp as occurreddate,
    ev.orgunit as organisationunit_uid,
    ev.status,
    case when ev.status = 'COMPLETED' then ev.clerk end as completedby,
    ev.completed as completeddate,
    g.datavalues,
    u.uid as createdby_uid,
    u.uid as lastupdatedby_uid,
    timestamp '1970-01-01' as lastsynchronized,
    au.uid as assigneduser_uid
from staged_event ev
join (select ev_key, datavalues from out_event) g using (ev_key)
join (select en_key, enrollmentid from staged_enrollment) e using (en_key)
join user_ref u on u.username = ev.clerk
left join user_ref au on au.username = ev.assigned
order by ev.eventid;

-- Each relationship has two items, from and to. DHIS2 joins the UIDs of the type, the from and the
-- to with _ into key, and of the type, the to and the from into inverted_key.
create or replace table staged_relationship as
select r.*, row_number() over (order by r.created, r.rel_key) as relationshipid,
       f.trackedentityid as from_id, t.trackedentityid as to_id
from out_relationship r
join staged_trackedentity f on f.te_key = r.from_te_key
join staged_trackedentity t on t.te_key = r.to_te_key;

create or replace table relationship as
select
    r.relationshipid,
    relationship_uid(r.relationshipid) as uid,
    r.created,
    r.created as lastupdated,
    u.uid as lastupdatedby_uid,
    r.relationship_type as relationshiptype_uid,
    (2 * r.relationshipid - 1)::integer as from_relationshipitemid,
    (2 * r.relationshipid)::integer as to_relationshipitemid,
    r.relationship_type || '_' || tracked_entity_uid(r.from_id) || '_'
        || tracked_entity_uid(r.to_id) as key,
    r.relationship_type || '_' || tracked_entity_uid(r.to_id) || '_'
        || tracked_entity_uid(r.from_id) as inverted_key,
    r.deleted
from staged_relationship r
join user_ref u on u.username = r.clerk
order by r.relationshipid;

create or replace table relationshipitem as
select *
from (
    select (2 * relationshipid - 1)::integer as relationshipitemid, relationshipid,
           from_id as trackedentityid
    from staged_relationship
    union all
    select (2 * relationshipid)::integer, relationshipid, to_id
    from staged_relationship
)
order by relationshipitemid;

-- The rows updated after their creation: a record a clerk came back to, whose lastupdated lies
-- more than an hour after its creation. An enrollment or a tracked entity is updated with each
-- event after its first, as DHIS2's importer updates them.
create or replace table updated as
select 'trackedentity' as tbl, trackedentityid as id, lastupdated
from trackedentity where lastupdated > created + interval 1 hour
union all
select 'enrollment', enrollmentid, lastupdated
from enrollment where lastupdated > created + interval 1 hour
union all
select 'event', eventid, lastupdated
from event where lastupdated > created + interval 1 hour;
