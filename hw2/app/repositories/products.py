"""Parameterized PostgreSQL queries. Transaction boundaries belong to the service."""
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping


PRODUCT_COLUMNS = "id, name, description, price, stock, category, status, seller_id, created_at, updated_at"


class ProductsRepository:
    def get_user(self, connection: Connection, user_id: UUID) -> RowMapping | None:
        # Keep the chosen user's role/existence stable until the operation ends.
        return connection.execute(text(
            "SELECT id, username, role FROM users WHERE id = :id FOR SHARE"
        ), {"id": user_id}).mappings().one_or_none()

    def create(self, connection: Connection, values: dict) -> RowMapping:
        return connection.execute(text(f"""
            INSERT INTO products (name, description, price, stock, category, status, seller_id)
            VALUES (:name, :description, :price, :stock, :category, :status, :seller_id)
            RETURNING {PRODUCT_COLUMNS}
        """), values).mappings().one()

    def get(self, connection: Connection, product_id: UUID, *, for_update: bool = False) -> RowMapping | None:
        lock = " FOR UPDATE" if for_update else ""
        return connection.execute(text(
            f"SELECT {PRODUCT_COLUMNS} FROM products WHERE id = :id{lock}"
        ), {"id": product_id}).mappings().one_or_none()

    def update(self, connection: Connection, product_id: UUID, values: dict) -> RowMapping:
        return connection.execute(text(f"""
            UPDATE products SET name = :name, description = :description,
                price = :price, stock = :stock, category = :category, status = :status
            WHERE id = :id
            RETURNING {PRODUCT_COLUMNS}
        """), {**values, "id": product_id}).mappings().one()

    def archive(self, connection: Connection, product_id: UUID) -> None:
        connection.execute(text(
            "UPDATE products SET status = 'ARCHIVED' WHERE id = :id"
        ), {"id": product_id})

    def list(self, connection: Connection, page: int, size: int,
             status: str | None, category: str | None) -> tuple[list[dict], int]:
        clauses = []
        parameters = {"limit": size, "offset": page * size}
        if status is not None:
            clauses.append("status = :status")
            parameters["status"] = status
        if category is not None:
            clauses.append("category = :category")
            parameters["category"] = category
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        # A single statement gives items and count the same PostgreSQL snapshot.
        # LEFT JOIN retains the total even when the requested page is empty.
        result = connection.execute(text(f"""
            WITH filtered AS (
                SELECT {PRODUCT_COLUMNS} FROM products{where}
            ), page_items AS (
                SELECT * FROM filtered ORDER BY created_at ASC, id ASC
                LIMIT :limit OFFSET :offset
            )
            SELECT page_items.*, totals.total_elements
            FROM (SELECT COUNT(*) AS total_elements FROM filtered) AS totals
            LEFT JOIN page_items ON TRUE
            ORDER BY page_items.created_at ASC, page_items.id ASC
        """), parameters).mappings().all()
        total = result[0]["total_elements"]
        items = [{key: value for key, value in row.items() if key != "total_elements"}
                 for row in result if row["id"] is not None]
        return items, total
