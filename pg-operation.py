import psycopg2
import os

# 連線設定
conn = psycopg2.connect(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", 5432),
    database=os.getenv("DB_NAME", "your_database"),
    user=os.getenv("DB_USER", "your_username"),
    password=os.getenv("DB_PASSWORD", "your_password"),
)

try:
    cursor = conn.cursor()

    # 先取得欄位名稱
    cursor.execute("SELECT * FROM users")
    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    # 印出欄位名稱
    print(" | ".join(columns))
    print("-" * 80)

    # 印出所有資料
    for row in rows:
        print(" | ".join(str(val) for val in row))

    print(f"\n共 {len(rows)} 筆資料")

finally:
    cursor.close()
    conn.close()