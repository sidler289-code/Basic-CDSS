"""Quick demo script — runs the CDSS pipeline without needing CLI arguments."""

import json
import sys
import os

# Ensure src is importable
sys.path.insert(0, os.path.dirname(__file__))

from src.main import CDSSPipeline


def main():
    pipeline = CDSSPipeline()
    pipeline.initialize()

    test_queries = [
        "68 岁高血压患者头晕 3 天，血压 178/96，要不要转诊？基层怎么处理？",
        "糖尿病患者吃二甲双胍，最近查出来肾功能不好，eGFR 28，还能继续吃吗？",
        "55 岁女性胸痛 30 分钟，伴有大汗，有高血压病史，怎么办？",
        "口干多饮多尿半个月，空腹血糖 8.5，是不是糖尿病？",
    ]

    for i, query in enumerate(test_queries, 1):
        print(f"\n{'=' * 70}")
        print(f"测试用例 {i}: {query}")
        print(f"{'=' * 70}")

        try:
            pack = pipeline.query(query)
            print(json.dumps(pack.to_dict(), ensure_ascii=False, indent=2))
        except Exception as e:
            import traceback
            print(f"ERROR: {e}")
            traceback.print_exc()

        if i < len(test_queries):
            print()


if __name__ == "__main__":
    main()
