# -*- coding: utf-8 -*-
"""二级分类：电视剧按地区、动漫按产地再分一层。

    电视剧：国产剧 / 欧美剧 / 港台剧 / 日韩剧 / 其他剧
    动漫  ：国产动漫 / 日韩动漫 / 欧美动漫 / 其他动漫

电影、短剧、综艺不分二级（返回空字符串）。

动漫为什么用 area 优先、lang 兜底：
    area 是产地（日本 / 中国大陆 / 美国…），覆盖率 99.6%；
    lang 是配音语言（日语 / 国语 / 英语…），覆盖率只有 26%，
    而且「日本动画的国语配音版」用 lang 会被误判成国产。
    所以先用 area 判产地，area 缺失时才用 lang 兜底。

站点给的 area 常是「英国,美国」这种多国合拍，用**子串匹配**（不是取第一段）；
SQL 版本用 LIKE，规则与 Python 版保持一致。
"""
TV_CAT = "电视剧"
ANIME_CAT = "动漫"

# ---------------- 电视剧：按地区 ----------------
# 顺序很重要：港台要排最前（"中国香港" 里也有 "中国"）
TV_GROUPS = [
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
TV_OTHER = "其他剧"

# ---------------- 动漫：按产地 ----------------
CN_AREA = ("中国大陆", "中国", "内地", "大陆", "中国香港", "香港", "中国台湾", "台湾", "澳门")
JPKR_AREA = ("日本", "韩国", "日韩", "朝鲜")
EN_AREA = ("美国", "英国", "法国", "德国", "意大利", "西班牙", "葡萄牙", "荷兰",
           "比利时", "卢森堡", "爱尔兰", "奥地利", "瑞士", "瑞典", "挪威",
           "丹麦", "芬兰", "冰岛", "波兰", "捷克", "匈牙利", "罗马尼亚",
           "保加利亚", "希腊", "克罗地亚", "俄罗斯", "乌克兰", "加拿大",
           "澳大利亚", "新西兰", "阿根廷", "巴西", "墨西哥", "智利",
           "哥伦比亚", "秘鲁", "欧美", "其他")

ANIME_GROUPS = [
    ("国产动漫", CN_AREA),
    ("日韩动漫", JPKR_AREA),
    ("欧美动漫", EN_AREA),
]
ANIME_OTHER = "其他动漫"

# lang 兜底用
JA = ("日语", "日語")
KO = ("韩语", "韓語", "朝鲜语")
EN = ("英语", "英語", "英文")
ZH = ("国语", "國語", "汉语", "漢語", "普通话", "粵語", "粤语", "闽南", "客家", "上海话", "东北")
_EMPTY_LANG = ("", "其他", "未知", "不详")

# 界面显示顺序
ORDER = {
    TV_CAT: [g[0] for g in TV_GROUPS] + [TV_OTHER],
    ANIME_CAT: [g[0] for g in ANIME_GROUPS] + [ANIME_OTHER],
}


def _has(text, keys):
    return any(k in text for k in keys)


def tv_sub(area):
    a = (area or "").strip()
    if not a:
        return TV_OTHER
    for name, keys in TV_GROUPS:
        if _has(a, keys):
            return name
    return TV_OTHER


def anime_sub(area, lang):
    a = (area or "").strip()
    # 1) 先看产地
    if a:
        for name, keys in ANIME_GROUPS:
            if _has(a, keys):
                return name
    # 2) 产地缺失或认不出来 → 用配音语言兜底
    l = (lang or "").strip()
    if l not in _EMPTY_LANG:
        if _has(l, JA) or _has(l, KO):
            return "日韩动漫"
        if _has(l, EN):
            return "欧美动漫"
        if _has(l, ZH):
            return "国产动漫"
    return ANIME_OTHER


def for_video(cat, area, lang=""):
    """返回二级分类名；不需要二级分类的返回 ""。"""
    c = (cat or "").strip()
    if c == TV_CAT:
        return tv_sub(area)
    if c == ANIME_CAT:
        return anime_sub(area, lang)
    return ""


# ---------------------------------------------------------------- SQL 版
def _like(col, keys):
    return "(" + " OR ".join("COALESCE(%s,'') LIKE '%%%s%%'" % (col, k) for k in keys) + ")"


def _not_empty(col):
    return "COALESCE(%s,'') NOT IN ('', '其他', '未知', '不详')" % col


def sql_case():
    """SQL 版同一个映射（供筛选/统计用），必须和上面的 Python 逻辑一致。"""
    cat = "COALESCE(NULLIF(cat,''),type_name)"
    parts = ["CASE"]

    # 电视剧
    parts.append("WHEN %s = '%s' THEN (CASE" % (cat, TV_CAT))
    for name, keys in TV_GROUPS:
        parts.append("  WHEN %s THEN '%s'" % (_like("area", keys), name))
    parts.append("  ELSE '%s' END)" % TV_OTHER)

    # 动漫：先 area，再 lang
    parts.append("WHEN %s = '%s' THEN (CASE" % (cat, ANIME_CAT))
    for name, keys in ANIME_GROUPS:
        parts.append("  WHEN %s THEN '%s'" % (_like("area", keys), name))
    parts.append("  WHEN %s AND %s THEN '日韩动漫'" % (_not_empty("lang"), _like("lang", JA + KO)))
    parts.append("  WHEN %s AND %s THEN '欧美动漫'" % (_not_empty("lang"), _like("lang", EN)))
    parts.append("  WHEN %s AND %s THEN '国产动漫'" % (_not_empty("lang"), _like("lang", ZH)))
    parts.append("  ELSE '%s' END)" % ANIME_OTHER)

    parts.append("ELSE '' END")
    return "\n         ".join(parts)


SQL = sql_case()
