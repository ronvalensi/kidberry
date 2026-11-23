"""Warning phrases (Hebrew + English + Spanish + Russian) checked against every transcript line.

Edit freely: each entry is (phrase, category, severity 1-5). Matching is case-insensitive.
Hebrew single words of 4+ letters also match with up to two prefix letters (ו/ה/ש/ל/כ/ב/מ),
e.g. "מטומטם" matches "והמטומטם".
After editing, re-run:  python -m kidberry.analyze --keywords-only
"""
import re

CATEGORIES = {
    "physical": "Physical harm",
    "threat": "Threat / intimidation",
    "insult": "Insult / humiliation",
    "secrecy": "Secrecy",
    "neglect": "Neglect / deprivation",
    "distress": "Child distress",
    "profanity": "Bad language near children",
}

KEYWORDS: list[tuple[str, str, int]] = [
    # ---- physical
    ("הרביץ", "physical", 4), ("הרביצה", "physical", 4), ("מרביץ", "physical", 4), ("מרביצה", "physical", 4),
    ("ארביץ", "physical", 4), ("מכה", "physical", 3), ("מכות", "physical", 3), ("סטירה", "physical", 4),
    ("צבט", "physical", 3), ("צבטה", "physical", 3), ("דחף", "physical", 3), ("דחפה", "physical", 3),
    ("משך לי", "physical", 3), ("משכה לי", "physical", 3), ("כואב לי", "physical", 3), ("זה כואב", "physical", 3),
    ("עזבי אותי", "physical", 3), ("עזוב אותי", "physical", 3), ("תעזבי אותי", "physical", 3), ("תעזוב אותי", "physical", 3),
    ("אל תיגע", "physical", 3), ("אל תיגעי", "physical", 3), ("אל תגע", "physical", 3),
    ("hit you", "physical", 4), ("hit me", "physical", 4), ("slap", "physical", 4), ("spank", "physical", 4),
    ("it hurts", "physical", 3), ("you're hurting", "physical", 4), ("let go of me", "physical", 3),
    ("don't touch", "physical", 3), ("pinch", "physical", 3), ("push", "physical", 2),
    # ---- threats
    ("תקבל מכה", "threat", 5), ("תקבלי מכה", "threat", 5), ("אני אעניש", "threat", 3), ("עונש", "threat", 2),
    ("לא תצא", "threat", 2), ("לא תצאי", "threat", 2), ("אני אשלח אותך", "threat", 3), ("אני אסגור אותך", "threat", 5),
    ("תיכנס לחדר", "threat", 2), ("אף אחד לא יאהב אותך", "threat", 5), ("אמא שלך לא תבוא", "threat", 5),
    ("you'll be punished", "threat", 3), ("i'll lock you", "threat", 5), ("nobody will love", "threat", 5),
    ("your mom won't come", "threat", 5), ("or else", "threat", 3),
    # ---- insults / humiliation
    ("טיפש", "insult", 3), ("טיפשה", "insult", 3), ("מטומטם", "insult", 4), ("מטומטמת", "insult", 4),
    ("אידיוט", "insult", 4), ("אידיוטית", "insult", 4), ("דביל", "insult", 4), ("דבילית", "insult", 4),
    ("מפגר", "insult", 4), ("מפגרת", "insult", 4), ("סתום", "insult", 3), ("סתמי", "insult", 3),
    ("שתוק", "insult", 3), ("שתקי", "insult", 3), ("תשתוק", "insult", 3), ("תשתקי", "insult", 3),
    ("סתום את הפה", "insult", 4), ("בכיין", "insult", 3), ("בכיינית", "insult", 3),
    ("מגעיל", "insult", 3), ("מגעילה", "insult", 3), ("מביך", "insult", 2), ("תתבייש", "insult", 3), ("תתביישי", "insult", 3),
    ("shut up", "insult", 3), ("stupid", "insult", 3), ("idiot", "insult", 4), ("dumb", "insult", 3),
    ("crybaby", "insult", 3), ("bad boy", "insult", 2), ("bad girl", "insult", 2), ("shame on you", "insult", 3),
    ("disgusting", "insult", 3),
    # ---- secrecy
    ("אל תספר", "secrecy", 4), ("אל תספרי", "secrecy", 4), ("אל תגיד לאמא", "secrecy", 5), ("אל תגידי לאמא", "secrecy", 5),
    ("אל תגיד לאבא", "secrecy", 5), ("אל תגידי לאבא", "secrecy", 5), ("סוד", "secrecy", 3), ("הסוד", "secrecy", 3), ("סוד שלנו", "secrecy", 5), ("הסוד שלנו", "secrecy", 5),
    ("בינינו", "secrecy", 3),
    ("don't tell", "secrecy", 4), ("our secret", "secrecy", 5), ("between us", "secrecy", 3),
    # ---- neglect / deprivation
    ("לא תאכל", "neglect", 3), ("לא תאכלי", "neglect", 3), ("בלי אוכל", "neglect", 3), ("אין לך אוכל", "neglect", 3),
    ("לא הולכים לשירותים", "neglect", 3), ("תחזיק", "neglect", 1), ("רעב", "neglect", 2), ("רעבה", "neglect", 2),
    ("no food", "neglect", 3), ("no lunch", "neglect", 3), ("hold it", "neglect", 2), ("i'm hungry", "neglect", 2),
    # ---- child distress
    ("אני מפחד", "distress", 3), ("אני מפחדת", "distress", 3), ("הצילו", "distress", 4), ("אני רוצה הביתה", "distress", 2),
    ("אני רוצה את אמא", "distress", 2),
    ("i'm scared", "distress", 3), ("help me", "distress", 3), ("i want my mom", "distress", 2), ("i want to go home", "distress", 2),
    # ---- Spanish (for staff who speak Spanish; Rioplatense forms included)
    ("te voy a pegar", "threat", 5), ("te pego", "threat", 4), ("cachetada", "physical", 4), ("chirlo", "physical", 4),
    ("le pegó", "physical", 4), ("me pegó", "physical", 4), ("me duele", "physical", 3), ("soltame", "physical", 3),
    ("suéltame", "physical", 3), ("no me toques", "physical", 3), ("pellizco", "physical", 3), ("empujón", "physical", 3),
    ("castigado", "threat", 2), ("castigada", "threat", 2), ("te voy a encerrar", "threat", 5),
    ("estúpido", "insult", 3), ("estúpida", "insult", 3), ("idiota", "insult", 4), ("tonto", "insult", 3),
    ("tonta", "insult", 3), ("boludo", "insult", 3), ("boluda", "insult", 3), ("pelotudo", "insult", 4),
    ("pelotuda", "insult", 4), ("callate", "insult", 3), ("cállate", "insult", 3), ("cerrá la boca", "insult", 4),
    ("cierra la boca", "insult", 4), ("llorón", "insult", 3), ("llorona", "insult", 3), ("qué asco", "insult", 3),
    ("insoportable", "insult", 3), ("mocoso", "insult", 3), ("mocosa", "insult", 3), ("malcriado", "insult", 3),
    ("malcriada", "insult", 3), ("vergüenza", "insult", 2),
    ("no le digas", "secrecy", 4), ("no digas nada", "secrecy", 4), ("es un secreto", "secrecy", 4),
    ("nuestro secreto", "secrecy", 5), ("no le cuentes", "secrecy", 4),
    ("sin comer", "neglect", 3), ("no vas a comer", "neglect", 3), ("dejalo llorar", "neglect", 3),
    ("que llore", "neglect", 2), ("tengo miedo", "distress", 3), ("ayuda", "distress", 2), ("quiero a mi mamá", "distress", 2),
    # ---- Russian (for staff who speak Russian; ё and е spellings both listed)
    ("ударил", "physical", 4), ("ударила", "physical", 4), ("бьёт", "physical", 4), ("бьет", "physical", 4),
    ("побью", "threat", 4), ("я тебя ударю", "threat", 5), ("отшлёпаю", "threat", 4), ("отшлепаю", "threat", 4),
    ("пощёчина", "physical", 4), ("пощечина", "physical", 4), ("ущипнул", "physical", 3), ("ущипнула", "physical", 3),
    ("толкнул", "physical", 3), ("толкнула", "physical", 3), ("мне больно", "physical", 3), ("больно", "physical", 2),
    ("отпусти", "physical", 3), ("не трогай", "physical", 3),
    ("накажу", "threat", 2), ("получишь", "threat", 3), ("запру", "threat", 4), ("я тебя убью", "threat", 4),
    ("дурак", "insult", 3), ("дура", "insult", 3), ("идиот", "insult", 4), ("тупой", "insult", 3), ("тупая", "insult", 3),
    ("заткнись", "insult", 4), ("закрой рот", "insult", 4), ("плакса", "insult", 3), ("бестолочь", "insult", 3),
    ("противный", "insult", 2), ("противная", "insult", 2), ("позор", "insult", 2), ("балбес", "insult", 2),
    ("не говори маме", "secrecy", 4), ("не говори папе", "secrecy", 4), ("никому не говори", "secrecy", 4),
    ("не рассказывай", "secrecy", 4), ("это секрет", "secrecy", 4), ("наш секрет", "secrecy", 5),
    ("без еды", "neglect", 3), ("не будешь есть", "neglect", 3), ("пусть плачет", "neglect", 3),
    ("мне страшно", "distress", 3), ("я боюсь", "distress", 3), ("помогите", "distress", 4), ("хочу к маме", "distress", 2),
    ("хочу домой", "distress", 2),
    # ---- bad language near children (not necessarily aimed at a child; severity 1-2)
    # Hebrew. Ambiguous words are avoided on purpose: "כוס" alone is also "a cup", "זין" also a letter.
    ("זונה", "profanity", 2), ("בן זונה", "profanity", 2), ("שרמוטה", "profanity", 2), ("כוס אמק", "profanity", 2),
    ("כוסאמק", "profanity", 2), ("כוס אמא שלך", "profanity", 2), ("כוס אוחתק", "profanity", 2), ("על הזין", "profanity", 2),
    ("יא זין", "profanity", 2), ("זין שלי", "profanity", 2), ("חרא", "profanity", 1), ("לעזאזל", "profanity", 1),
    ("מניאק", "profanity", 2), ("מניאקית", "profanity", 2), ("שיט", "profanity", 1), ("בליאט", "profanity", 2),
    ("יא אללה כוס", "profanity", 2), ("תזדיין", "profanity", 2), ("לך תזדיין", "profanity", 2),
    # English
    ("fuck", "profanity", 2), ("fucking", "profanity", 2), ("shit", "profanity", 1), ("bitch", "profanity", 2),
    ("asshole", "profanity", 2), ("bastard", "profanity", 2), ("damn it", "profanity", 1), ("goddamn", "profanity", 1),
    # Spanish (incl. Rioplatense)
    ("cojones", "profanity", 2), ("joder", "profanity", 2), ("mierda", "profanity", 1), ("carajo", "profanity", 1),
    ("la puta", "profanity", 2), ("puta madre", "profanity", 2), ("hijo de puta", "profanity", 2), ("puto", "profanity", 2),
    ("concha de", "profanity", 2), ("la concha", "profanity", 2), ("forro", "profanity", 1), ("chupame", "profanity", 2),
    ("me cago", "profanity", 2), ("coño", "profanity", 2), ("pendejo", "profanity", 2),
    # Russian
    ("блять", "profanity", 2), ("блядь", "profanity", 2), ("бля", "profanity", 2), ("сука", "profanity", 2),
    ("хуй", "profanity", 2), ("нахуй", "profanity", 2), ("пиздец", "profanity", 2), ("ебать", "profanity", 2),
    ("твою мать", "profanity", 1), ("чёрт", "profanity", 1), ("черт", "profanity", 1), ("говно", "profanity", 1),
    ("дерьмо", "profanity", 1),
]

_PREFIX = "[והשלכבמ]{0,2}"   # e.g. ו+ה in "והמטומטם"
_HEB = "א-ת"


def _compile(phrase: str) -> re.Pattern:
    if re.search(f"[{_HEB}]", phrase):
        body = re.escape(phrase)
        if " " not in phrase and len(phrase) >= 4:   # short words + prefixes collide with other words
            body = _PREFIX + body
        return re.compile(f"(?<![{_HEB}]){body}(?![{_HEB}])")
    return re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)


_COMPILED = [(p, _compile(p), cat, sev) for p, cat, sev in KEYWORDS]


def find(text: str) -> list[tuple[str, str, int]]:
    """Return (phrase, category, severity) for every warning phrase in text."""
    return [(p, cat, sev) for p, rx, cat, sev in _COMPILED if text and rx.search(text)]
