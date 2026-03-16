"""
migrate_json_to_sqlite.py
─────────────────────────────────────────────
기존 channel_configs.json → SQLite 마이그레이션 스크립트.

사용법:
  python migrate_json_to_sqlite.py

또는 JSON 파일 경로를 직접 지정:
  python migrate_json_to_sqlite.py /path/to/channel_configs.json

Railway 배포 시에는 배포 후 1회만 실행하면 됩니다.
이미 DB에 같은 channel_id 가 있으면 JSON 값으로 덮어씁니다.
"""

import sys, json, os

# db.py 와 같은 폴더에서 실행한다고 가정
import db


def migrate(json_path: str) -> None:
    if not os.path.exists(json_path):
        print(f"❌ 파일을 찾을 수 없습니다: {json_path}")
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)

    if not configs:
        print("⚠️  JSON 파일이 비어있습니다. 마이그레이션할 데이터가 없습니다.")
        return

    # DB 초기화 (테이블 생성)
    db.init_db()

    migrated = 0
    for channel_id, cfg in configs.items():
        db.update_channel_config(channel_id, {
            "threshold":   cfg.get("threshold", 0.85),
            "digest_hour": cfg.get("digest_hour", 18),
            "red_mention": cfg.get("red_mention", "here"),
            "admin_users": cfg.get("admin_users", []),
        })
        migrated += 1
        print(f"  ✅ {channel_id} — threshold={cfg.get('threshold')}, "
              f"digest_hour={cfg.get('digest_hour')}, "
              f"red_mention={cfg.get('red_mention')}")

    print(f"\n🎉 마이그레이션 완료: {migrated}개 채널 설정을 SQLite에 저장했습니다.")
    print(f"   DB 경로: {db.DB_PATH}")
    print(f"\n💡 기존 channel_configs.json 은 백업 후 삭제해도 됩니다.")


if __name__ == "__main__":
    json_path = sys.argv[1] if len(sys.argv) > 1 else "channel_configs.json"
    print(f"📦 마이그레이션 시작: {json_path} → SQLite")
    print(f"   DB 경로: {db.DB_PATH}")
    print()
    migrate(json_path)
