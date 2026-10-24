-- Question: Where does every employee sit in the reporting chain, and how deep?
-- Pattern: Recursive CTE walking a self-referencing table, carrying depth and a path
-- Why it matters: the anchor member must terminate the recursion on its own -- here
--   "manager_id IS NULL" picks the single root. The path column is the trick that
--   makes the output sortable into tree order, which a depth column alone cannot do.
-- Portable: WITH RECURSIVE is standard SQL and runs unchanged on Postgres and SQLite.

WITH RECURSIVE org AS (
    -- Anchor: the root of the tree.
    SELECT
        e.employee_id,
        e.name,
        e.title,
        1                    AS depth,
        CAST(e.name AS VARCHAR) AS reporting_path
    FROM employees e
    WHERE e.manager_id IS NULL

    UNION ALL

    -- Recursive member: everyone reporting to a row already in `org`.
    SELECT
        e.employee_id,
        e.name,
        e.title,
        o.depth + 1,
        o.reporting_path || ' > ' || e.name
    FROM employees e
    JOIN org o ON e.manager_id = o.employee_id
)

SELECT employee_id, name, title, depth, reporting_path
FROM org
WHERE depth <= 3        -- the analyst layer is omitted to keep the result readable
ORDER BY reporting_path;
