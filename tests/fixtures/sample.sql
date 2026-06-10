-- Sample SQL fixture for code-review-graph parser tests

CREATE TABLE users (
    id       INTEGER PRIMARY KEY,
    name     TEXT    NOT NULL,
    email    TEXT    UNIQUE
);

CREATE TABLE orders (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id),
    total      NUMERIC(10, 2),
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE VIEW active_orders AS
    SELECT o.id, u.name, o.total
    FROM orders o
    JOIN users u ON u.id = o.user_id
    WHERE o.total > 0;

CREATE FUNCTION get_user_total(p_user_id INTEGER)
RETURNS NUMERIC AS $$
    SELECT SUM(total)
    FROM orders
    WHERE user_id = p_user_id;
$$ LANGUAGE sql;

CREATE OR REPLACE PROCEDURE archive_old_orders(cutoff_date DATE)
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO orders_archive
    SELECT * FROM orders WHERE created_at < cutoff_date;

    DELETE FROM orders WHERE created_at < cutoff_date;
END;
$$;
