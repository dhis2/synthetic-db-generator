-- Whether a value parses as its value type, as DHIS2's importer would accept it. A value type not
-- listed fails, so a field of a new type needs it added here.
--
-- Inputs: orgunit and user_ref, the org units and users of the build (../generate.py).
create or replace macro valid_value(vt, v) as case vt
    when 'BOOLEAN' then v in ('true', 'false')
    when 'TRUE_ONLY' then v = 'true'
    when 'DATE' then try_strptime(v, '%Y-%m-%d') is not null
    when 'AGE' then try_strptime(v, '%Y-%m-%d') is not null
    when 'DATETIME' then try_strptime(v, '%Y-%m-%dT%H:%M:%S.%g') is not null
    when 'TIME' then regexp_full_match(v, '([01][0-9]|2[0-3]):[0-5][0-9]')
    when 'INTEGER_POSITIVE' then regexp_full_match(v, '[1-9][0-9]*')
        and try_cast(v as integer) is not null
    when 'INTEGER_ZERO_OR_POSITIVE' then regexp_full_match(v, '0|[1-9][0-9]*')
        and try_cast(v as integer) is not null
    when 'INTEGER_NEGATIVE' then regexp_full_match(v, '-[1-9][0-9]*')
        and try_cast(v as integer) is not null
    when 'INTEGER' then regexp_full_match(v, '0|-?[1-9][0-9]*')
        and try_cast(v as integer) is not null
    when 'NUMBER' then regexp_full_match(v, '-?[0-9]+([.][0-9]+)?')
    when 'UNIT_INTERVAL' then regexp_full_match(v, '[0-9]+([.][0-9]+)?')
        and coalesce(try_cast(v as double) between 0 and 1, false)
    when 'PERCENTAGE' then regexp_full_match(v, '[0-9]+([.][0-9]+)?')
        and coalesce(try_cast(v as double) between 0 and 100, false)
    when 'TEXT' then length(v) <= 50000
    when 'LONG_TEXT' then true
    when 'LETTER' then regexp_full_match(v, '[A-Za-z]')
    -- DHIS2's ValidationUtils: digits, spaces and +()#./-, 6 to 50 of them.
    when 'PHONE_NUMBER' then regexp_full_match(v, '[0-9+()#./ext -]{6,50}')
    when 'EMAIL' then regexp_full_match(v, '[^@ ]+@[^@ ]+[.][^@ ]+')
    when 'URL' then regexp_full_match(v, 'https?://[^ ]+')
    when 'ORGANISATION_UNIT' then v in (select uid from orgunit where kept)
    when 'USERNAME' then v in (select username from user_ref)
    else false
end;
