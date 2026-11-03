# -*- coding: utf-8 -*-
"""二级分类：只对「电视剧」按地区再分一层。

    国产剧 / 欧美剧 / 港台剧 / 日韩剧 / 其他剧

电影、短剧、综艺、动漫不分二级（返回空字符串）。

站点给的 area 常是「英国,美国」这种多国合拍，取**第一个**地区作为归属；
SQL 版本用 LIKE 按优先级判断，两边规则保持一致。
"""
import re

TV_CAT = "电视剧"

# 顺序很重要：
#   1) 港台要排在最前（"中国香港" 里也有 "中国"）
#   2) 日韩排在欧美前（避免误判）
GROUPS = [
    ("港台剧", ("中国香港", "香港", "中国台湾", "台湾", "中国澳门", "澳门", "港台")),
    ("日韩剧", ("日本", "韩国", "日韩")),
    ("国产剧", ("中国大陆", "中国", "内地", "大陆")),
    ("欧美剧", ("美国", "英国", "法国", "德国", "意大利", "西班牙", "葡萄牙",
                "荷兰", "比利时", "卢森堡", "爱尔兰", "奥地利", "瑞士",
                "瑞典", "挪威", "丹麦", "芬兰", "冰岛", "波兰", "捷克",
                "匈牙利", "罗马尼亚", "保加利亚", "希腊", "克罗地亚",
                "俄罗斯", "乌克兰", "加拿大", "澳大利亚", "新西兰",
                "阿根廷", "巴西", "墨西哥", "智利", "哥伦比亚", "秘鲁", "欧美")),
]
OTHER = "其他剧"

# 展示顺序
ORDER = ["国产剧", "欧美剧", "港台剧", "日韩剧", OTHER]


def _first_area(area):
    a = (area or "").strip()
    if not a:
        return ""
    return re.split(r"[,，/、|;；]", a)[0].strip()


def for_video(cat, area):
    """返回二级分类名；非电视剧返回 ""。

    用「整个 area 字符串做子串匹配」而不是只取第一段 ——
    这样和 SQL 里的 LIKE 判定完全一致，界面统计和导出目录不会对不上。
    """
    if (cat or "").strip() != TV_CAT:
        return ""
    a = (area or "").strip()
    if not a:
        return OTHER
    for name, keys in GROUPS:
        for k in keys:
            if k in a:
                return name
    return OTHER


def sql_case():
    """SQL 版同一个映射（供筛选/统计用）。"""
    parts = ["CASE WHEN COALESCE(NULLIF(cat,''),type_name) <> '%s' THEN ''" % TV_CAT]
    for name, keys in GROUPS:
        cond = " OR ".join("COALESCE(area,'') LIKE '%%%s%%'" % k for k in keys)
        parts.append("WHEN %s THEN '%s'" % (cond, name))
    parts.append("ELSE '%s' END" % OTHER)
    return " ".join(parts)


SQL = sql_case()
