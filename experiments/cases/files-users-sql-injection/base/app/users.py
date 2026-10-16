import sqlite3


def find_by_id(conn: sqlite3.Connection, user_id: int):
    """Return the user row with this id, or None."""
    return conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()
