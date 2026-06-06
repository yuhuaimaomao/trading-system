"""
监管函 PDF 分析服务
功能：解析 PDF 内容，提取摘要，风险审计
"""

import logging
import os
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def _get_pdfplumber():
    """懒加载 pdfplumber，只在真正需要解析 PDF 时才导入和报错。"""
    try:
        import pdfplumber
        return pdfplumber
    except ImportError:
        logger.warning("⚠️ pdfplumber 未安装，无法解析 PDF")
        return None


class RegulatoryAnalysisService:
    """监管函 PDF 分析服务"""

    def __init__(self):
        # 风险知识库（整合 Gemini 版本）
        self.risk_library = {
            # Gemini 版本（简洁直接）
            "合理性": {
                "level": 3,
                "stars": "⭐⭐⭐",
                "meaning": "数据存疑，编造痕迹明显",
                "strategy": "警惕，观察回复是否含糊",
            },
            "是否存在": {
                "level": 4,
                "stars": "⭐⭐⭐⭐",
                "meaning": "怀疑利益输送或资金占用",
                "strategy": "高危，自证清白难度大",
            },
            "真实性": {
                "level": 5,
                "stars": "⭐⭐⭐⭐⭐",
                "meaning": "怀疑虚构收入或利润，财务造假嫌疑",
                "strategy": "极危，建议立即离场",
            },
            "信息披露不准确": {
                "level": 4,
                "stars": "⭐⭐⭐⭐",
                "meaning": "前期存在撒谎行为，诚信破产",
                "strategy": "后续可能面临立案，避开",
            },
            "持续经营能力": {
                "level": 5,
                "stars": "⭐⭐⭐⭐⭐",
                "meaning": "资不抵债，面临退市风险",
                "strategy": "坚决不碰，远离垃圾股",
            },
            "履行信披义务": {
                "level": 2,
                "stars": "⭐⭐",
                "meaning": "违规隐瞒，被监管发现",
                "strategy": "轻微违规，观察后续合规性",
            },
            # 我的版本（更详细）
            "立案调查": {
                "level": 5,
                "stars": "⭐⭐⭐⭐⭐",
                "meaning": "已被监管机构立案",
                "strategy": "最高风险，立即远离",
            },
            "行政处罚": {
                "level": 5,
                "stars": "⭐⭐⭐⭐⭐",
                "meaning": "已被行政处罚",
                "strategy": "重大利空，坚决不碰",
            },
            "虚假记载": {
                "level": 5,
                "stars": "⭐⭐⭐⭐⭐",
                "meaning": "财务造假实锤",
                "strategy": "退市风险，坚决不碰",
            },
            "误导性陈述": {
                "level": 4,
                "stars": "⭐⭐⭐⭐",
                "meaning": "信息披露存在误导",
                "strategy": "诚信问题，谨慎对待",
            },
            "重大遗漏": {
                "level": 4,
                "stars": "⭐⭐⭐⭐",
                "meaning": "隐瞒重要信息",
                "strategy": "信披违规，谨慎对待",
            },
            "说明...的合理性": {
                "level": 3,
                "stars": "⭐⭐⭐",
                "meaning": "你的数据太假了，正常人都觉得不合理，快编个理由",
                "strategy": "警惕，观察回复是否含糊其辞",
            },
            "核实...真实性": {
                "level": 5,
                "stars": "⭐⭐⭐⭐⭐",
                "meaning": "我们不信你的财报，怀疑你虚构收入或利润",
                "strategy": "极危。通常伴随股价大跌，建议立即离场",
            },
        }

        # 监管机构映射
        self.issuer_map = {
            "上海证券交易所": "上交所",
            "深圳证券交易所": "深交所",
            "北京证券交易所": "北交所",
            "中国证监会": "证监会",
            "上海证监局": "上海局",
            "深圳证监局": "深圳局",
            "北京证监局": "北京局",
            "大连证监局": "大连局",
        }

        logger = __import__("logging").getLogger(__name__)
        self.logger = logger

    def analyze_title(self, title: str) -> Dict:
        """
        分析公告标题（预扫描风险）

        Args:
            title: 公告标题

        Returns:
            风险分析结果
        """
        # 去除 HTML 标签
        title_clean = re.sub(r"<[^>]+>", "", title)

        result = {
            "risk_level": 1,
            "risk_stars": "⭐",
            "risk_summary": "",
            "risk_keywords": [],
            "alerts": [],
        }

        # 匹配风险关键词
        for key, info in self.risk_library.items():
            if key in title_clean:
                result["risk_keywords"].append(key)
                result["alerts"].append(
                    {
                        "keyword": key,
                        "level": info["level"],
                        "stars": info["stars"],
                        "interpretation": info["meaning"],
                    }
                )

                # 更新最高风险等级
                if info["level"] > result["risk_level"]:
                    result["risk_level"] = info["level"]
                    result["risk_stars"] = info["stars"]
                    result["risk_summary"] = info["meaning"]

        return result

    def extract_pdf_text(self, pdf_filepath: str, max_pages: int = 5) -> Optional[str]:
        """
        提取 PDF 文本内容

        Args:
            pdf_filepath: PDF 文件路径
            max_pages: 最大提取页数（默认 5 页）

        Returns:
            文本内容，失败返回 None
        """
        plumber = _get_pdfplumber()
        if not plumber:
            return None

        if not pdf_filepath or not os.path.exists(pdf_filepath):
            self.logger.warning(f"PDF 文件不存在：{pdf_filepath}")
            return None

        try:
            text_content = ""

            with plumber.open(pdf_filepath) as pdf:
                # 提取前 N 页文本（通常关键信息在前几页）
                for page in pdf.pages[:max_pages]:
                    text = page.extract_text()
                    if text:
                        text_content += text + "\n"

            if not text_content.strip():
                self.logger.warning(f"PDF 内容为空：{pdf_filepath}")
                return None

            return text_content

        except Exception as e:
            self.logger.error(f"PDF 文本提取失败：{e}")
            return None

    def analyze_content(self, text: str) -> Dict:
        """
        分析 PDF 内容

        Args:
            text: PDF 文本内容

        Returns:
            内容分析结果
        """
        result = {
            "word_count": len(text),
            "keywords": [],
            "risk_type": "",
            "issuer": "",
            "issuer_short": "",
            "recipient": "",
            "issue_date": "",
            "pdf_summary": "",
        }

        # 提取关键词
        for key in self.risk_library.keys():
            if key in text:
                result["keywords"].append(key)

        # 提取监管机构
        issuer_patterns = [
            r"(上海证券交易所|深圳证券交易所|北京证券交易所)",
            r"(上海证监局|深圳证监局|北京证监局|大连证监局)",
            r"(中国证监会)",
        ]
        for pattern in issuer_patterns:
            match = re.search(pattern, text)
            if match:
                full_name = match.group(1)
                result["issuer"] = full_name
                result["issuer_short"] = self.issuer_map.get(full_name, full_name)
                break

        # 提取接收对象
        recipient_match = re.search(r"(?:致 | 发给 | 关于)(.+?)(?:公司 | 股份)", text)
        if recipient_match:
            result["recipient"] = recipient_match.group(1) + "公司"

        # 提取发文日期
        date_match = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)", text)
        if date_match:
            result["issue_date"] = date_match.group(1)

        # 判断风险类型
        if "财务造假" in result["keywords"] or "虚假记载" in text:
            result["risk_type"] = "财务造假"
        elif "立案调查" in result["keywords"]:
            result["risk_type"] = "立案调查"
        elif "监管函" in result["keywords"]:
            result["risk_type"] = "监管函"
        elif "问询函" in result["keywords"]:
            result["risk_type"] = "问询函"
        else:
            result["risk_type"] = "其他"

        # 生成摘要（前 500 字）
        result["pdf_summary"] = (
            text[:500].strip() + "..." if len(text) > 500 else text.strip()
        )

        return result

    def analyze_pdf(self, pdf_filepath: str) -> Optional[Dict]:
        """
        分析 PDF 文件（完整流程）

        Args:
            pdf_filepath: PDF 文件路径

        Returns:
            分析结果，失败返回 None
        """
        if not pdf_filepath or not os.path.exists(pdf_filepath):
            return None

        # 提取文本
        text = self.extract_pdf_text(pdf_filepath)

        if not text:
            return None

        # 分析内容
        content_result = self.analyze_content(text)

        # 合并结果
        result = {
            "pdf_summary": content_result["pdf_summary"],
            "word_count": content_result["word_count"],
            "risk_type": content_result["risk_type"],
            "issuer": content_result["issuer"],
            "issuer_short": content_result["issuer_short"],
            "recipient": content_result["recipient"],
            "issue_date": content_result["issue_date"],
            "keywords": content_result["keywords"],
        }

        self.logger.info(f"✅ PDF 分析成功：{os.path.basename(pdf_filepath)}")
        return result


# ========== 测试入口 ==========

if __name__ == "__main__":
    service = RegulatoryAnalysisService()

    # 测试 PDF 分析
    test_pdf = os.path.expanduser(
        "~/trading-system/data/bulletin_pdf/regulatory_letter/1225030779.PDF.pdf"
    )

    if os.path.exists(test_pdf):
        print(f"测试 PDF: {test_pdf}")
        result = service.analyze_pdf(test_pdf)

        if result:
            print("\n✅ 分析成功！")
            print(f"风险类型：{result['risk_type']}")
            print(f"发文机构：{result['issuer_short']}")
            print(f"关键词：{', '.join(result['keywords'])}")
            print(f"字数：{result['word_count']}")
            print("\n摘要预览:")
            print(result["pdf_summary"][:300])
        else:
            print("❌ 分析失败")
    else:
        print(f"测试 PDF 不存在：{test_pdf}")
        print("请先运行采集器下载 PDF")
