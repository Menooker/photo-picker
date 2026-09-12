import asyncio
import argparse
import sys

from .cli import PhotoPicker


def main():
    parser = argparse.ArgumentParser(
        description="Photo Picker - iPhone 照片智能分类工具",
        prog="photo-picker"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    sub.add_parser("list", help="列出 DCIM 顶级目录")

    # classify
    p_classify = sub.add_parser("classify", help="分类指定目录的照片")
    p_classify.add_argument("top_dir", help="DCIM 顶级目录（如 100APPLE）")
    p_classify.add_argument("--count", "-n", type=int, default=None,
                            help="仅处理前 N 张照片")
    p_classify.add_argument("--start-from", help="从指定文件名开始")
    p_classify.add_argument("--llm-url", default="http://localhost:8080/v1", help="LLM API 地址")
    p_classify.add_argument("--llm-model", default="default", help="模型名称")
    p_classify.add_argument("--max-thinking-tokens", type=int, default=6000,
                            help="llama.cpp thinking token 预算（-1 不限，0 关闭思考）")

    # serve
    p_serve = sub.add_parser("serve", help="启动 Web 界面（FastAPI）")
    p_serve.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_serve.add_argument("--port", type=int, default=8000, help="监听端口")
    p_serve.add_argument("--llm-url", default="http://localhost:8080/v1", help="LLM API 地址")
    p_serve.add_argument("--llm-model", default="default", help="模型名称")
    p_serve.add_argument("--max-thinking-tokens", type=int, default=4000,
                         help="llama.cpp thinking token 预算（-1 不限，0 关闭思考）")

    args = parser.parse_args()

    if args.command == "list":
        asyncio.run(_list())
    elif args.command == "classify":
        asyncio.run(_classify(args))
    elif args.command == "serve":
        from .web import run as run_web
        run_web(host=args.host, port=args.port, llm_url=args.llm_url,
                llm_model=args.llm_model,
                max_thinking_tokens=args.max_thinking_tokens)


async def _list():
    with PhotoPicker() as picker:
        try:
            await picker.connect()
            await picker.list_dirs()
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


async def _classify(args):
    with PhotoPicker(llm_url=args.llm_url, llm_model=args.llm_model,
                     max_thinking_tokens=args.max_thinking_tokens) as picker:
        try:
            await picker.connect()
            await picker.classify(args.top_dir, count=args.count,
                                  start_from=args.start_from)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
