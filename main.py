from weban.app.bootstrap import run_app

VERSION = "v4.0.0"


def main() -> int:
    """主程序入口。"""
    return run_app(VERSION)


if __name__ == "__main__":
    raise SystemExit(main())
