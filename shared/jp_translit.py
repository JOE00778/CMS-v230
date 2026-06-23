"""日文商品名 → 英文标题（离线机械转写·无 API/无网络/无新依赖）.

用途：商品登録(page 15) 给 JD「平台商品标题」/ BM「英文名称」自动补一个**草稿英文标题**，
用户在表里过一眼再改。质量非 AI，靠 3 层：
  1. 外来语/品牌/常用词 词典（katakana/kanji 短语 → 英文，最长匹配）—— 主力，化妆品命中高
  2. 片假名/平假名 → 罗马字 兜底（词典未命中的假名）
  3. 罗马字/数字/尺寸（DHC, 1.5g, 0.38mm 等）原样保留

Boss 2026-06-23 拍板：英文标题走离线机械转写（不用 AI API）。词典可随时扩。
"""
from __future__ import annotations

import re

# ── 1. 外来语/品牌/常用词 词典（短语优先·最长匹配） ─────────────────────
# key 必含假名或汉字；纯英文不用进（原样保留）。值为英文。可自由扩充。
TERMS: dict[str, str] = {
    # 化妆品·形态
    "リップクリーム": "Lip Balm", "リップグロス": "Lip Gloss", "リップ": "Lip",
    "クリーム": "Cream", "パウダー": "Powder", "ファンデーション": "Foundation",
    "アイシャドウ": "Eyeshadow", "アイライナー": "Eyeliner", "マスカラ": "Mascara",
    "チーク": "Blush", "口紅": "Lipstick", "コンシーラー": "Concealer",
    "化粧水": "Toner", "乳液": "Emulsion", "美容液": "Serum", "美容オイル": "Beauty Oil",
    "クレンジング": "Cleansing", "洗顔料": "Face Wash", "洗顔": "Face Wash",
    "シャンプー": "Shampoo", "コンディショナー": "Conditioner", "トリートメント": "Treatment",
    "ハンドクリーム": "Hand Cream", "ボディクリーム": "Body Cream", "ボディ": "Body",
    "ヘアオイル": "Hair Oil", "ヘア": "Hair", "オイル": "Oil", "ジェル": "Gel",
    "ミスト": "Mist", "スプレー": "Spray", "ローション": "Lotion", "バーム": "Balm",
    "パック": "Pack", "マスク": "Mask", "シート": "Sheet", "フェイス": "Face",
    "日焼け止め": "Sunscreen", "下地": "Base", "プライマー": "Primer", "リムーバー": "Remover",
    # 质感·颜色
    "シアー": "Sheer", "マット": "Matte", "グロス": "Gloss", "ツヤ": "Glossy",
    "カラー": "Color", "レッド": "Red", "ピンク": "Pink", "ベージュ": "Beige",
    "ボルドー": "Bordeaux", "ローズ": "Rose", "オレンジ": "Orange", "ブラウン": "Brown",
    "ブラック": "Black", "ホワイト": "White", "ブルー": "Blue", "グリーン": "Green",
    "イエロー": "Yellow", "パープル": "Purple", "クリア": "Clear", "ゴールド": "Gold",
    "シルバー": "Silver", "ナチュラル": "Natural",
    # 香り·属性
    "の香り": "Scent", "香り": "Scent", "限定": "Limited", "数量限定": "Limited Edition",
    "セット": "Set", "詰め替え": "Refill", "つめかえ": "Refill", "本体": "Main",
    "増量": "Extra", "大容量": "Large", "携帯": "Portable", "ミニ": "Mini",
    # 日用·文具·家电（上传样本里出现）
    "電動ドライバー": "Electric Driver", "ドライバー": "Driver", "電動": "Electric",
    "コンパクト": "Compact", "プラス": "Plus", "ケース": "Case", "ボトル": "Bottle",
    "ボールペン": "Ballpoint Pen", "ボール": "Ball", "ペン": "Pen", "ノート": "Notebook",
    "タオル": "Towel", "ブラシ": "Brush", "コーム": "Comb", "ミラー": "Mirror",
    "ナイフ": "Knife", "はさみ": "Scissors", "つめきり": "Nail Clipper",
    # 品牌（片假名→英文官方·罗马字会出错的才进）
    "ビクトリノックス": "Victorinox", "資生堂": "Shiseido", "花王": "Kao",
    "キンモクセイ": "Osmanthus", "すっぴん": "No-Makeup",
}

# ── 2. 假名 → 罗马字（兜底·Hepburn 近似） ────────────────────────────
_YOUON = {
    "キャ": "kya", "キュ": "kyu", "キョ": "kyo", "シャ": "sha", "シュ": "shu", "ショ": "sho",
    "チャ": "cha", "チュ": "chu", "チョ": "cho", "ニャ": "nya", "ニュ": "nyu", "ニョ": "nyo",
    "ヒャ": "hya", "ヒュ": "hyu", "ヒョ": "hyo", "ミャ": "mya", "ミュ": "myu", "ミョ": "myo",
    "リャ": "rya", "リュ": "ryu", "リョ": "ryo", "ギャ": "gya", "ギュ": "gyu", "ギョ": "gyo",
    "ジャ": "ja", "ジュ": "ju", "ジョ": "jo", "ビャ": "bya", "ビュ": "byu", "ビョ": "byo",
    "ピャ": "pya", "ピュ": "pyu", "ピョ": "pyo", "ティ": "ti", "ディ": "di", "デュ": "du",
    "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo", "ウィ": "wi", "ウェ": "we", "ヴ": "vu",
}
_KANA = {
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    "ワ": "wa", "ヲ": "wo", "ン": "n",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
}
# 丢弃的助词（标题里是噪音）
_DROP_KANA = {"の", "と", "は", "が", "を", "に", "へ", "で", "や", "ー", "・", "　"}


def _hira_to_kata(s: str) -> str:
    """平假名 → 片假名（统一走片假名罗马字表）。"""
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)


def _kana_run_to_romaji(run: str) -> str:
    """一段假名 → 罗马字（youon/sokuon 处理）。"""
    run = _hira_to_kata(run)
    out, i, n = [], 0, len(run)
    while i < n:
        c = run[i]
        if c == "ッ":  # 促音 → 下一辅音重复
            nxt = run[i + 1:i + 3]
            r = _YOUON.get(nxt) or _KANA.get(run[i + 1] if i + 1 < n else "", "")
            if r:
                out.append(r[0])
            i += 1
            continue
        two = run[i:i + 2]
        if two in _YOUON:
            out.append(_YOUON[two]); i += 2; continue
        if c in _KANA:
            out.append(_KANA[c]); i += 1; continue
        if c in ("ー", "・"):
            i += 1; continue
        out.append(c); i += 1
    return "".join(out)


_KANA_RE = re.compile(r"[぀-ヿ]")        # 平/片假名
_JP_RE = re.compile(r"[぀-ヿ一-鿿]")  # 假名+汉字
# 词典 key 按长度降序（最长匹配）
_TERMS_SORTED = sorted(TERMS.items(), key=lambda kv: -len(kv[0]))


def to_english_title(name: str, *, max_len: int = 200) -> str:
    """日文商品名 → 英文标题草稿。命中词典优先，假名兜底罗马字，罗马字/数字原样。"""
    if not name or not str(name).strip():
        return ""
    s = str(name).strip()
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        # 1) 词典最长匹配（含汉字短语）
        hit = None
        for k, v in _TERMS_SORTED:
            if s.startswith(k, i):
                hit = (k, v); break
        if hit:
            out.append(hit[1]); i += len(hit[0]); continue
        c = s[i]
        # 2) 助词/连字符丢弃
        if c in _DROP_KANA:
            i += 1; continue
        # 3) 连续假名 → 罗马字（遇到词典可匹配位置就断开，让串中间的品牌/外来语被命中）
        if _KANA_RE.match(c):
            j = i
            while j < n and _KANA_RE.match(s[j]) and s[j] not in _DROP_KANA:
                if j > i and any(s.startswith(k, j) for k, _ in _TERMS_SORTED):
                    break
                j += 1
            rom = _kana_run_to_romaji(s[i:j])
            if rom:
                out.append(rom.capitalize())
            i = j; continue
        # 4) 汉字未命中 → 跳过（无读音表·不杜撰）
        if "一" <= c <= "鿿":
            i += 1; continue
        # 5) 罗马字/数字/符号 → 收集成一段原样
        j = i
        while j < n and not _JP_RE.match(s[j]) and s[j] not in _DROP_KANA:
            j += 1
        seg = s[i:j].strip()
        if seg:
            out.append(seg)
        i = max(j, i + 1)
    # 清洗：折叠空格 + 限长
    title = re.sub(r"\s+", " ", " ".join(out)).strip()
    return title[:max_len].strip()
