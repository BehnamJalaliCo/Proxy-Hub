-- Exact row count of every user table, one "schema.table<TAB>rows" line each, sorted.
-- backup.sh runs it inside the SAME snapshot as pg_dump (the manifest); restore_test.sh runs it on
-- the restored copy and the two outputs must be identical.
SELECT format('%I.%I', table_schema, table_name) || E'\t' ||
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
                           false, true, '')))[1]::text
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1;
