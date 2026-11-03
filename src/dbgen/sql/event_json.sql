-- eventdatavalues as DHIS2 writes it, from the compact columns of event.parquet: an object
-- keyed by data element UID, each value with the event's times and clerk (DHIS2's EventDataValue).
-- For example, the event
--
--   datavalues            [{k: 'de1', v: '120'}, {k: 'de2', v: 'true'}]
--   created               2024-03-01 10:15:00
--   lastupdated           2024-03-01 10:20:00
--   createdbyuserinfo     {"id":7,"username":"clerk_a"}
--   lastupdatedbyuserinfo {"id":7,"username":"clerk_a"}
--
-- gives, on one line:
--
--   {"de1":{"value":"120","created":"2024-03-01T10:15:00.000",
--           "lastUpdated":"2024-03-01T10:20:00.000","providedElsewhere":false,
--           "createdByUserInfo":{"id":7,"username":"clerk_a"},
--           "lastUpdatedByUserInfo":{"id":7,"username":"clerk_a"}},
--    "de2":{"value":"true","created":"2024-03-01T10:15:00.000", and so on}}
--
-- The text is concatenated rather than built with to_json over nested structs, which is several
-- times faster; Postgres normalises jsonb anyway. rest, what follows each value, is the same for
-- all values of an event, so it is built once per event: mapping a one-element list binds it to a
-- name, as SQL has no let.

create or replace macro event_json(datavalues, created, lastupdated, createdbyuserinfo,
                                   lastupdatedbyuserinfo) as
    list_transform(
        [',"created":"' || strftime(created, '%Y-%m-%dT%H:%M:%S.%g')
            || '","lastUpdated":"' || strftime(lastupdated, '%Y-%m-%dT%H:%M:%S.%g')
            || '","providedElsewhere":false'
            || ',"createdByUserInfo":' || createdbyuserinfo
            || ',"lastUpdatedByUserInfo":' || lastupdatedbyuserinfo || '}'],
        lambda rest: '{' || array_to_string(
            list_transform(datavalues,
                lambda x: to_json(x.k) || ':{"value":' || to_json(x.v) || rest),
            ',') || '}'
    )[1];
