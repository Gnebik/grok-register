"""手工批量转换 accounts.txt 里尚未转换的 SSO → CPA build token。
用法: python manual_convert.py [数量]  (默认转最后 6 个)
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DEVICE_PROXY", "http://127.0.0.1:7891")

from device_mint import sso_to_device
from sso_to_cpa import save_auth


def load_accounts():
    acc_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys", "accounts.txt")
    accounts = []
    with open(acc_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) >= 3 and parts[2].startswith("eyJ"):
                accounts.append({"email": parts[0], "sso": parts[2]})
    return accounts


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    accounts = load_accounts()[-n:]
    print(f"待转换: {len(accounts)} 个")
    ok = 0
    for i, acc in enumerate(accounts, 1):
        email, sso = acc["email"], acc["sso"]
        print(f"\n[{i}/{len(accounts)}] {email}")
        result = sso_to_device(sso, email)
        if result:
            save_auth(email, result)
            ok += 1
            print(f"  ✅ {email} 转换成功")
        else:
            print(f"  ❌ {email} 转换失败")
        time.sleep(2)
    print(f"\n完成: {ok}/{len(accounts)} 成功")


if __name__ == "__main__":
    main()
