from psycopg import Connection


def fetch_projects(conn: Connection, owner_user_id: int):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, name, mode, rule_id, provider, created_at, utterance_log FROM projects "
            "WHERE owner_user_id = %s AND deleted_at IS NULL ORDER BY id",
            (owner_user_id,),
        )
        return cursor.fetchall()


def project_exists(conn: Connection, project_id: int, owner_user_id: int) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1 FROM projects WHERE id = %s AND owner_user_id = %s AND deleted_at IS NULL",
                       (project_id, owner_user_id))
        return cursor.fetchone() is not None
