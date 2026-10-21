\set ON_ERROR_STOP on
\pset pager off
\echo === Flyway migrations ===
SELECT
    installed_rank,
    version,
    description,
    script,
    success
FROM flyway_schema_history
ORDER BY installed_rank;


SELECT tablename
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;


SELECT
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND tablename IN (
      'products',
      'orders',
      'order_items',
      'user_operations'
  )
ORDER BY tablename, indexname;


SELECT
    event_object_table,
    trigger_name,
    event_manipulation,
    action_timing
FROM information_schema.triggers
WHERE trigger_schema = 'public'
ORDER BY event_object_table, trigger_name;

\echo === Users: passwords and hashes are excluded ===
SELECT id, username, role FROM users ORDER BY created_at DESC, id DESC LIMIT 20;

\echo === Products: archived rows remain here ===
SELECT id, name, price, stock, status, category, seller_id, created_at, updated_at
FROM products ORDER BY updated_at DESC, id DESC LIMIT 20;

\echo === Orders: totals, owner and status ===
SELECT id, user_id, status, promo_code_id, total_amount, discount_amount, created_at, updated_at
FROM orders ORDER BY updated_at DESC, id DESC LIMIT 20;

\echo === Items: price snapshots ===
SELECT i.order_id, i.product_id, i.quantity, i.price_at_order,
       p.price AS current_catalog_price, i.quantity * i.price_at_order AS line_amount
FROM order_items i JOIN products p ON p.id = i.product_id
JOIN orders o ON o.id = i.order_id
ORDER BY o.updated_at DESC, o.id DESC, i.product_id LIMIT 30;

\echo === Promos: counters ===
SELECT id, code, discount_type, discount_value, min_order_amount, current_uses,
       max_uses, active, valid_from, valid_until
FROM promo_codes ORDER BY created_at DESC, id DESC LIMIT 20;

\echo === Operations: create and update have separate histories ===
SELECT u.username, op.user_id, op.operation_type, op.created_at
FROM user_operations op JOIN users u ON u.id = op.user_id
ORDER BY op.created_at DESC, op.id DESC LIMIT 30;

\echo === Active-order invariant: this query should return no rows ===
SELECT user_id, count(*) FROM orders WHERE status IN ('CREATED', 'PAYMENT_PENDING')
GROUP BY user_id HAVING count(*) > 1;
