CREATE INDEX idx_products_status
    ON products(status);

CREATE INDEX idx_products_seller_id
    ON products(seller_id);

CREATE INDEX idx_orders_user_id
    ON orders(user_id);

CREATE INDEX idx_orders_promo_code_id
    ON orders(promo_code_id);

CREATE INDEX idx_order_items_product_id
    ON order_items(product_id);

CREATE INDEX idx_user_operations_latest
    ON user_operations(
        user_id,
        operation_type,
        created_at DESC
    );

CREATE UNIQUE INDEX uq_orders_one_active_per_user
    ON orders(user_id)
    WHERE status IN ('CREATED', 'PAYMENT_PENDING');