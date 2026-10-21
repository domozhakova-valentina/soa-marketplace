CREATE TYPE user_role AS ENUM (
    'USER', 'SELLER', 'ADMIN'
);

CREATE TYPE product_status AS ENUM (
    'ACTIVE', 'INACTIVE', 'ARCHIVED'
);

CREATE TYPE order_status AS ENUM (
    'CREATED',
    'PAYMENT_PENDING',
    'PAID',
    'SHIPPED',
    'COMPLETED',
    'CANCELED'
);

CREATE TYPE discount_type AS ENUM (
    'PERCENTAGE', 'FIXED_AMOUNT'
);

CREATE TYPE operation_type AS ENUM (
    'CREATE_ORDER', 'UPDATE_ORDER'
);


CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role user_role NOT NULL DEFAULT 'USER',

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_users_username
        CHECK (username ~ '^[A-Za-z0-9_.-]{3,64}$'),

    CONSTRAINT chk_users_password_hash
        CHECK (char_length(password_hash) > 0)
);


CREATE TABLE products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    name VARCHAR(255) NOT NULL,
    description VARCHAR(4000),
    price NUMERIC(12, 2) NOT NULL,
    stock INTEGER NOT NULL,
    category VARCHAR(100) NOT NULL,
    status product_status NOT NULL,

    seller_id UUID NOT NULL REFERENCES users(id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_products_name
        CHECK (char_length(name) >= 1),

    CONSTRAINT chk_products_price
        CHECK (price >= 0.01),

    CONSTRAINT chk_products_stock
        CHECK (stock >= 0),

    CONSTRAINT chk_products_category
        CHECK (char_length(category) >= 1)
);


CREATE TABLE promo_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    code VARCHAR(20) NOT NULL UNIQUE,
    discount_type discount_type NOT NULL,
    discount_value NUMERIC(12, 2) NOT NULL,
    min_order_amount NUMERIC(12, 2) NOT NULL,

    max_uses INTEGER NOT NULL,
    current_uses INTEGER NOT NULL DEFAULT 0,

    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_promo_code
        CHECK (code ~ '^[A-Z0-9_]{4,20}$'),

    CONSTRAINT chk_promo_discount_value
        CHECK (discount_value > 0),

    CONSTRAINT chk_promo_min_order_amount
        CHECK (min_order_amount >= 0),

    CONSTRAINT chk_promo_max_uses
        CHECK (max_uses > 0),

    CONSTRAINT chk_promo_current_uses
        CHECK (
            current_uses >= 0
            AND current_uses <= max_uses
        ),

    CONSTRAINT chk_promo_dates
        CHECK (valid_until > valid_from)
);


CREATE TABLE orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    user_id UUID NOT NULL REFERENCES users(id),
    status order_status NOT NULL DEFAULT 'CREATED',
    promo_code_id UUID REFERENCES promo_codes(id),

    total_amount NUMERIC(12, 2) NOT NULL,
    discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT chk_orders_total_amount
        CHECK (total_amount >= 0),

    CONSTRAINT chk_orders_discount_amount
        CHECK (discount_amount >= 0),

    CONSTRAINT chk_orders_discount_without_promo
        CHECK (
            promo_code_id IS NOT NULL
            OR discount_amount = 0
        )
);


CREATE TABLE order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    order_id UUID NOT NULL REFERENCES orders(id),
    product_id UUID NOT NULL REFERENCES products(id),

    quantity INTEGER NOT NULL,
    price_at_order NUMERIC(12, 2) NOT NULL,

    CONSTRAINT chk_order_items_quantity
        CHECK (quantity BETWEEN 1 AND 999),

    CONSTRAINT chk_order_items_price
        CHECK (price_at_order >= 0.01),

    CONSTRAINT uq_order_items_product
        UNIQUE (order_id, product_id)
);


CREATE TABLE user_operations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    user_id UUID NOT NULL REFERENCES users(id),
    operation_type operation_type NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);