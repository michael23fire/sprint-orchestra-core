"""
Cache Behavior Test Script
測試 Spring Boot API 的 Redis cache 和 PostgreSQL 行為

需要先安裝：
  pip install requests psycopg2-binary redis

使用方式：
  python test_cache.py
"""

import requests
import psycopg2
import redis
import json
import time

# ============================================================
# ⚙️  設定區 - 請根據你的環境修改
# ============================================================

# Spring Boot API
API_BASE_URL = "http://localhost:8080"

# PostgreSQL
PG_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "pocdb",   # ← 只需要改這裡
    "user": "postgres",           # ← 預設 postgres user
    "password": "postgres"        # ← 改成你的 password
}

# Redis
REDIS_CONFIG = {
    "host": "localhost",
    "port": 6379,
    "password": None,  # 沒有密碼就保持 None
    "db": 0
}

# ============================================================
# 🔧 工具函數
# ============================================================

def print_section(title):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")

def print_step(step, msg):
    print(f"\n[Step {step}] {msg}")

def get_pg_connection():
    return psycopg2.connect(**PG_CONFIG)

def get_redis_connection():
    return redis.Redis(
        host=REDIS_CONFIG["host"],
        port=REDIS_CONFIG["port"],
        password=REDIS_CONFIG["password"],
        db=REDIS_CONFIG["db"],
        decode_responses=True
    )

# ============================================================
# 📋 直接查 PostgreSQL
# ============================================================

def check_postgres_user(user_id):
    print(f"  📦 [PostgreSQL] 查詢 user id={user_id}...")
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            print(f"  ✅ PostgreSQL 找到資料: {row}")
        else:
            print(f"  ⚠️  PostgreSQL 沒有 id={user_id} 的資料")
        cur.close()
        conn.close()
        return row
    except Exception as e:
        print(f"  ❌ PostgreSQL 連線失敗: {e}")
        return None

def check_all_postgres_users():
    print(f"  📦 [PostgreSQL] 查詢所有 users...")
    try:
        conn = get_pg_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users")
        rows = cur.fetchall()
        print(f"  ✅ PostgreSQL 共有 {len(rows)} 筆資料:")
        for row in rows:
            print(f"     {row}")
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        print(f"  ❌ PostgreSQL 連線失敗: {e}")
        return []

# ============================================================
# 📋 直接查 Redis
# ============================================================

def check_redis_cache(user_id):
    """
    Spring Cache 預設的 key 格式是:  users::1  (value "users", key "#id")
    """
    r = get_redis_connection()
    cache_key = f"users::{user_id}"
    print(f"  🔴 [Redis] 查詢 key: '{cache_key}'...")
    try:
        value = r.get(cache_key)
        if value:
            print(f"  ✅ Redis cache 命中！內容: {value}")
            return value
        else:
            print(f"  ⚪ Redis cache 沒有這筆資料 (cache miss)")
            return None
    except Exception as e:
        print(f"  ❌ Redis 連線失敗: {e}")
        return None

def list_all_redis_keys():
    r = get_redis_connection()
    print(f"  🔴 [Redis] 列出所有 keys...")
    try:
        keys = r.keys("*")
        if keys:
            print(f"  ✅ Redis 共有 {len(keys)} 個 key:")
            for k in keys:
                ttl = r.ttl(k)
                val = r.get(k)
                print(f"     key={k}  TTL={ttl}s  value={val[:80] if val else None}...")
        else:
            print(f"  ⚪ Redis 目前是空的")
        return keys
    except Exception as e:
        print(f"  ❌ Redis 連線失敗: {e}")
        return []

# ============================================================
# 📋 呼叫 Spring Boot API
# ============================================================

def api_create_user(name, email):
    print(f"  🌐 [API] POST /users  name={name}, email={email}")
    try:
        res = requests.post(f"{API_BASE_URL}/users", json={"name": name, "email": email})
        res.raise_for_status()
        user = res.json()
        print(f"  ✅ 建立成功: {user}")
        return user
    except Exception as e:
        print(f"  ❌ API 呼叫失敗: {e}")
        return None

def api_get_user(user_id):
    print(f"  🌐 [API] GET /users/{user_id}")
    try:
        res = requests.get(f"{API_BASE_URL}/users/{user_id}")
        res.raise_for_status()
        user = res.json()
        print(f"  ✅ 取得資料: {user}")
        return user
    except Exception as e:
        print(f"  ❌ API 呼叫失敗: {e}")
        return None

def api_delete_user(user_id):
    print(f"  🌐 [API] DELETE /users/{user_id}")
    try:
        res = requests.delete(f"{API_BASE_URL}/users/{user_id}")
        res.raise_for_status()
        print(f"  ✅ 刪除成功 (cache 應該被 evict)")
        return True
    except Exception as e:
        print(f"  ❌ API 呼叫失敗: {e}")
        return False

# ============================================================
# 🧪 測試案例
# ============================================================

def test_cache_miss_then_hit():
    """
    測試 @Cacheable 行為：
    第一次 GET → cache miss → 打 DB → 存進 Redis
    第二次 GET → cache hit  → 直接從 Redis 拿，不打 DB
    """
    print_section("Test 1: Cache Miss → Cache Hit (@Cacheable)")

    # Step 1: 建立一個 user
    print_step(1, "建立新 User")
    user = api_create_user("TestUser", "test@example.com")
    if not user:
        print("  ⛔ 無法建立 user，跳過此測試")
        return
    user_id = user.get("id") or user.get("userId")

    # Step 2: 確認 Redis 還沒有 cache（剛建立，還沒 GET 過）
    print_step(2, "確認 Redis 目前沒有 cache（預期 cache miss）")
    check_redis_cache(user_id)

    # Step 3: 第一次 GET → 應該打 DB，然後存進 Redis
    print_step(3, "第一次 GET /users/{id}（預期：打 DB，log 會印出查詢訊息）")
    api_get_user(user_id)
    time.sleep(0.5)  # 等 Spring 把資料寫進 Redis

    # Step 4: 確認 Redis 現在有 cache 了
    print_step(4, "確認 Redis 現在有 cache（預期 cache hit）")
    check_redis_cache(user_id)

    # Step 5: 第二次 GET → 應該從 Redis 拿，不打 DB（Spring Boot log 不會印查詢）
    print_step(5, "第二次 GET /users/{id}（預期：從 Redis 拿，不打 DB）")
    print("  💡 請觀察 Spring Boot console，這次應該不會印出 '>>> 從 DB 查詢'")
    api_get_user(user_id)

    # Step 6: 同時確認 PostgreSQL 資料也在
    print_step(6, "確認 PostgreSQL 資料正確")
    check_postgres_user(user_id)

    return user_id


def test_cache_evict_on_delete(user_id):
    """
    測試 @CacheEvict 行為：
    DELETE user → Redis cache 被清除
    """
    print_section("Test 2: Cache Evict on Delete (@CacheEvict)")

    if not user_id:
        print("  ⛔ 沒有 user_id，跳過此測試")
        return

    # Step 1: 確認 Redis 有 cache
    print_step(1, "刪除前確認 Redis 有 cache")
    check_redis_cache(user_id)

    # Step 2: 刪除 user
    print_step(2, f"刪除 user id={user_id}")
    api_delete_user(user_id)
    time.sleep(0.5)

    # Step 3: 確認 Redis cache 被清除
    print_step(3, "確認 Redis cache 已被清除（預期 cache miss）")
    check_redis_cache(user_id)

    # Step 4: 確認 PostgreSQL 資料也被刪除
    print_step(4, "確認 PostgreSQL 資料也被刪除")
    check_postgres_user(user_id)


def test_overview():
    """顯示目前 Redis 和 PostgreSQL 的完整狀態"""
    print_section("Overview: 目前 Redis & PostgreSQL 狀態")
    print("\n  --- Redis ---")
    list_all_redis_keys()
    print("\n  --- PostgreSQL ---")
    check_all_postgres_users()


# ============================================================
# 🚀 主程式
# ============================================================

if __name__ == "__main__":
    print("\n🚀 開始測試 Redis Cache + PostgreSQL 行為\n")
    print("💡 同時開著 Spring Boot console，觀察 '>>> 從 DB 查詢' log 是否出現\n")

    # 先看初始狀態
    test_overview()

    # 測試 cache miss → hit
    user_id = test_cache_miss_then_hit()

    # 測試 cache evict
    test_cache_evict_on_delete(user_id)

    # 最後再看一次狀態
    print_section("最終狀態")
    test_overview()

    print("\n✅ 測試完成！\n")