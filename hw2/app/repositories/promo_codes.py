"""Promo creation: let PostgreSQL arbitrate concurrent code conflicts."""
from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping


PROMO_COLUMNS = ("id, code, discount_type, discount_value, min_order_amount, max_uses, "
                 "current_uses, valid_from, valid_until, active")


class PromoCodesRepository:
    def create(self, connection: Connection, values: dict) -> RowMapping | None:
        # Only a duplicate code is a known conflict. Other database errors must
        # propagate as INTERNAL_ERROR and roll back the service transaction.
        return connection.execute(text(f"""
            INSERT INTO promo_codes (code, discount_type, discount_value,
                min_order_amount, max_uses, valid_from, valid_until, active)
            VALUES (:code, :discount_type, :discount_value, :min_order_amount,
                :max_uses, :valid_from, :valid_until, :active)
            ON CONFLICT (code) DO NOTHING
            RETURNING {PROMO_COLUMNS}
        """), values).mappings().one_or_none()
