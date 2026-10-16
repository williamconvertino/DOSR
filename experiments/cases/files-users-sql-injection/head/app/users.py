import sqlite3


def find_by_id(conn: sqlite3.Connection, user_id: int):
    """Return the user row with this id, or None."""
    return conn.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,)).fetchone()


def find_by_name(conn: sqlite3.Connection, name: str):
    """Return all users whose name matches."""
    return conn.execute(f"SELECT id, name, email FROM users WHERE name = '{name}'").fetchall()
