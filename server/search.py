"""Offline Chinese/English sound vocabulary, shared by online and local search.

Each concept has one query term. Aliases are for recognition, never appended as
mandatory AND terms to a Freesound query. Add vocabulary by adding a row below.
"""
from __future__ import annotations

import re
import unicodedata

# canonical English query | Chinese aliases | English aliases
_ROWS = """
sword|剑 铁剑 长剑 刀 刀剑 刀刃|sword swords blade blades
dagger|匕首 短刀|dagger daggers
axe|斧头 战斧 斧|axe axes
shield|盾牌 盾|shield shields
bow|弓 长弓 弓箭|bow bows
arrow|箭 箭矢|arrow arrows
crossbow|弩 弩箭|crossbow crossbows
weapon|武器 兵器|weapon weapons wpn
gun|枪 枪械 火器|gun guns firearm firearms
gunshot|枪声 开枪 射击 枪击|gunshot gunshots gunfire
pistol|手枪|pistol pistols handgun handguns
rifle|步枪|rifle rifles
shotgun|霰弹枪 散弹枪|shotgun shotguns
machinegun|机枪|machinegun machineguns
reload|装弹 换弹 上膛|reload reloading
bullet|子弹 弹壳|bullet bullets cartridge cartridges
explosion|爆炸 爆破 炸裂|explosion explosions explode
bomb|炸弹 手雷|bomb bombs grenade grenades
cannon|火炮 大炮 炮击 炮弹 炮彈|cannon cannons
battle|战斗 战场 战争 打斗|battle battles combat fighting
impact|撞击 碰撞 击打 打击 敲击 撞 敲 击中 砍到 砍中|impact impacts hit hits strike strikes collision collisions
slash|挥砍 斩击 劈砍 砍劈 切割|slash slashes slicing
whoosh|挥动 挥剑 破空 嗖 呼啸 挥舞|whoosh whooshes swoosh swooshes swing swings
stab|刺击 刺入 穿刺|stab stabbing
block|格挡 招架 防御|block blocking parry
punch|拳击 拳头 拳 打拳|punch punches
kick|踢 踢击|kick kicks
hurt|受伤 疼痛 伤害|hurt pain damage
death|死亡 倒地|death dying
metal|金属 铁 钢 铜|metal metallic steel iron
wood|木头 木质 木板 木制 木地 木地板|wood wooden timber
stone|石头 石板 石地 石 石材|stone stones rocky
glass|玻璃|glass
water|水 水面 水中|water watery
ice|冰块 冰 冰面|ice icy
snow|雪地 雪|snow snowy
sand|沙地 沙子 沙漠 沙|sand sandy desert
gravel|碎石 砂砾|gravel pebbles
dirt|泥土 土地 泥地|dirt soil earth
mud|泥浆 泥泞|mud muddy
grass|草地 草丛 草|grass grassy
fabric|布料 衣服 布|fabric cloth clothing
leather|皮革|leather
plastic|塑料|plastic
chain|锁链 链条 铁链|chain chains
rope|绳索 绳子|rope ropes
footstep|脚步 脚步声 足音 步伐 踩踏|footstep footsteps step steps foot
walk|行走 走路 步行|walk walking
run|奔跑 跑步 疾跑|run running sprint sprinting
jump|跳跃 起跳 跳|jump jumps jumping
land|落地 着陆|land landing
swim|游泳|swim swimming
climb|攀爬 爬行|climb climbing crawling
horse|马匹 马 蹄声 马蹄|horse horses hoof hooves
dog|狗 犬 狗叫|dog dogs bark barking
cat|猫 猫叫|cat cats meow meowing
bird|鸟 鸟叫 鸟鸣|bird birds chirp chirping
wolf|狼 狼嚎|wolf wolves howl howling
lion|狮子|lion lions
bear|熊|bear bears
insect|昆虫 虫 虫鸣|insect insects bug bugs
cricket|蟋蟀|cricket crickets
bee|蜜蜂|bee bees
frog|青蛙 蛙叫|frog frogs croak croaking
dragon|巨龙 龙|dragon dragons
monster|怪物 怪兽 兽人|monster monsters creature creatures
zombie|僵尸 丧尸|zombie zombies
goblin|哥布林 地精|goblin goblins
roar|咆哮 吼叫 怒吼|roar roaring growl growling
breath|呼吸 喘气|breath breathing panting
scream|尖叫 惨叫|scream screaming shriek
whisper|耳语 低语|whisper whispers whispering
laugh|笑声 大笑|laugh laughing laughter
heartbeat|心跳|heartbeat heartbeats
ui|界面 用户界面 界面音效|ui interface
button|按钮 按键|button buttons
click|点击 点按|click clicks clicking
hover|悬停 鼠标滑过|hover hovering
confirmation|确认 确定|confirmation confirm accept
cancel|取消 返回|cancel back
error|错误 失败|error failure fail
success|成功 胜利|success victory win
notification|提示 通知 提醒|notification notifications alert
menu|菜单|menu menus
select|选择 选中|select selection
inventory|背包 物品栏|inventory
coin|金币 硬币 钱币|coin coins
pickup|拾取 收集 获得|pickup collect collecting
chest|宝箱 箱子|chest chests
door|门 大门 房门|door doors
open|打开 开启|open opening
close|关闭 关上|close closing
lock|锁 上锁 解锁|lock unlock
levelup|升级 升级音效|levelup level-up
achievement|成就 解锁成就|achievement achievements
magic|魔法 法术 魔力|magic magical spell spells
cast|施法 释放|cast casting
fireball|火球 火球术|fireball fireballs
fire|火 火焰 燃烧 烈火|fire flames flame burning
electricity|闪电 雷电 电 电流 电击|electricity electric lightning zap
heal|治疗 治愈 恢复|heal healing
teleport|传送 瞬移|teleport teleportation
portal|传送门|portal portals
charge|蓄力 充能 充电|charge charging
powerup|强化 增益|powerup power-up
laser|激光|laser lasers
beam|光束 射线|beam beams
scifi|科幻 未来科技|scifi sci-fi futuristic
robot|机器人 机械人|robot robots robotic
engine|引擎 发动机|engine engines motor motors
machine|机械 机器|machine machinery mechanical
spaceship|飞船 宇宙飞船|spaceship spaceships spacecraft
ambience|环境 环境音 氛围 背景 环境声|ambience ambiance ambient atmosphere ambi
forest|森林 丛林 树林|forest woods jungle
wind|风 风声 大风 刮风|wind windy
rain|雨 雨声 下雨 雨滴|rain raining rainfall
thunder|雷 雷声 打雷|thunder thunderstorm
storm|暴风雨 风暴|storm storms
ocean|海洋 海浪 大海 海边|ocean sea waves surf
river|河流 河水 溪流 小溪|river stream creek
waterfall|瀑布|waterfall waterfalls
drip|滴水 水滴|drip dripping drops
splash|溅水 水花|splash splashes splashing
bubble|气泡 冒泡|bubble bubbles bubbling
cave|洞穴 山洞|cave caves cavern
dungeon|地牢 地下城|dungeon dungeons
city|城市 街道|city urban street streets
crowd|人群 群众|crowd crowds people
traffic|交通 车流|traffic
car|汽车 车辆 车|car cars vehicle vehicles
train|火车|train trains
boat|船 船只|boat boats ship ships
plane|飞机|plane planes airplane aircraft
space|太空 宇宙|space cosmic
underwater|水下|underwater submerged
break|破碎 碎裂 打碎 断裂|break breaking shatter shattering smash
creak|吱呀 嘎吱|creak creaking squeak squeaky
rattle|咔嗒 哗啦|rattle rattling
vibration|震动 振动|vibration vibrating resonance
bell|钟声 铃声 铃铛|bell bells chime chimes
beep|哔声 嘟声 电子提示|beep beeps bleep
alarm|警报 警笛|alarm siren
drum|鼓 鼓声|drum drums
short|短促 短 短暂|short brief
heavy|厚重 沉重 重型|heavy weighty
light|轻盈 轻微|light soft gentle
deep|低沉 低频|deep bass low
sharp|清脆 尖锐|sharp crisp
echo|回声 混响|echo echoes reverb
loop|循环 持续 循环播放|loop loops looping continuous
retro|复古 像素 八位|retro 8bit 8-bit pixel
cartoon|卡通 动画|cartoon cartoonish
horror|恐怖 惊悚 阴森|horror scary spooky
fantasy|奇幻 魔幻|fantasy
realistic|真实 写实|realistic realism
"""

_TRAD = str.maketrans(dict(pair for pair in (
    "劍剑", "鐵铁", "長长", "頭头", "戰战", "弓弓", "槍枪", "聲声", "開开", "擊击",
    "彈弹", "機机", "裝装", "換换", "殼壳", "炸炸", "場场", "爭争", "鬥斗", "揮挥",
    "斬斩", "動动", "禦御", "傷伤", "屬属", "鋼钢", "銅铜", "質质", "塊块", "麵面",
    "礫砾", "漿浆", "濘泞", "叢丛", "鎖锁", "鏈链", "條条", "繩绳", "腳脚", "躍跃",
    "陸陆", "馬马", "貓猫", "鳥鸟", "鳴鸣", "獅狮", "蟲虫", "龍龙", "獸兽", "屍尸",
    "喪丧", "嚨咙", "氣气", "語语", "鈕钮", "鍵键", "點点", "懸悬", "標标", "過过",
    "確确", "認认", "錯错", "誤误", "敗败", "勝胜", "單单", "選选", "擇择", "欄栏",
    "幣币", "錢钱", "獲获", "寶宝", "門门", "關关", "閉闭", "級级", "術术", "釋释",
    "電电", "閃闪", "療疗", "癒愈", "復复", "傳传", "強强", "環环", "圍围", "風风",
    "樹树", "葉叶", "邊边", "車车", "輛辆", "飛飞", "異异", "後后", "鐘钟", "鈴铃",
    "報报", "銳锐", "頻频", "響响", "續续", "實实", "寫写", "擬拟", "製制", "為为",
)))
_CJK = re.compile(r"[\u3400-\u9fff]")
_WORDS = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*|[\u3400-\u9fff]+")
_EN_STOP = set("a an the of on in into with and or to from for sound sounds effect effects sfx audio please some i want need like very".split())
_ZH_STOP = sorted("我想要 我需要 帮我 给我 比如 一个 一把 一种 一些 一下 时候 听起来 音效 声音 效果 请 的 地 得 和 与 有 在 上 下 里 中 是 要 很 更 点 把 了 着 到".split(), key=len, reverse=True)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).lower().translate(_TRAD)


_EN: dict[str, str] = {}
_ZH: dict[str, str] = {}
for _row in _ROWS.strip().splitlines():
    _canonical, _zh, _en = _row.split("|")
    for _word in (_canonical + " " + _en).split():
        _EN[_word] = _canonical
    for _word in _zh.split():
        _ZH[_normalize(_word)] = _canonical
_ZH_SORTED = sorted(_ZH, key=len, reverse=True)


def _english(word: str) -> str:
    if word in _EN:
        return _EN[word]
    # Only stem to a known concept: never turn e.g. glass into 'glas'.
    candidates = [word[:-3] + "y"] if word.endswith("ies") else []
    if word.endswith("es"):
        candidates.append(word[:-2])
    if word.endswith("s"):
        candidates.append(word[:-1])
    return next((_EN[item] for item in candidates if item in _EN), word)


def _extract(text: str) -> tuple[list[str], list[str]]:
    terms: list[str] = []
    unknown: list[str] = []
    for word in _WORDS.findall(_normalize(text)):
        if not _CJK.search(word):
            if word not in _EN_STOP:
                terms.append(_english(word))
            continue
        pos = 0
        pending = ""
        while pos < len(word):
            hit = next((alias for alias in _ZH_SORTED if word.startswith(alias, pos)), None)
            stop = next((alias for alias in _ZH_STOP if word.startswith(alias, pos)), None)
            if hit or stop:
                if pending:
                    terms.append(pending)
                    unknown.append(pending)
                    pending = ""
                # A longer filler wins over a single-character dictionary match.
                if hit and (not stop or len(hit) >= len(stop)):
                    terms.append(_ZH[hit])
                    pos += len(hit)
                else:
                    pos += len(stop)
            else:
                pending += word[pos]
                pos += 1
        if pending:
            terms.append(pending)
            unknown.append(pending)
    return list(dict.fromkeys(terms)), list(dict.fromkeys(unknown))


def expand_query(text: str) -> dict:
    """Return the actual compact query plus honest translation coverage metadata."""
    original = str(text).strip()
    terms, unknown = _extract(original)
    result = {
        "original": original,
        "translated": " ".join(terms),
        "terms": terms,
        "method": "词库翻译" if _CJK.search(original) else "英文关键词",
    }
    warnings = []
    if unknown:
        warnings.append("词库未覆盖：" + "、".join(unknown) + "；已保留原词，可修改关键词或启用文本 AI 整理。")
    if len(terms) > 6:
        warnings.append("关键词较多会缩小结果范围，建议保留最关键的 2–4 个词。")
    if original and not terms:
        result["translated"] = original
        result["terms"] = [original]
        warnings.append("未识别到具体音效词，已保留输入；试试“金属撞击 / metal impact”。")
    if warnings:
        result["warning"] = " ".join(warnings)
    return result


def local_match(query: str, text: str) -> bool:
    """AND concepts, OR aliases; supports Chinese queries against English metadata."""
    if not query.strip():
        return True
    normalized_text = _normalize(text)
    normalized_query = _normalize(query).strip()
    # Do not match English 'cat' inside 'notification'; CJK still supports
    # substring matching for user-created names outside the sound vocabulary.
    if re.search(r"(?<![a-z0-9])" + re.escape(normalized_query) + r"(?![a-z0-9])", normalized_text):
        return True
    query_terms, _ = _extract(query)
    text_terms, _ = _extract(text)
    if not query_terms:
        return False
    available = set(text_terms)
    return all(term in available or (_CJK.search(term) and term in normalized_text) for term in query_terms)
