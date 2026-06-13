import os

STOPWORDS_FILE = "stopwords.txt"

def load_stopwords():
    if not os.path.exists(STOPWORDS_FILE):
        return []
    with open(STOPWORDS_FILE, "r", encoding="utf-8") as f:
        return [line.strip().lower() for line in f if line.strip() and not line.strip().startswith("#")]

def save_stopwords(words):
    with open(STOPWORDS_FILE, "w", encoding="utf-8") as f:
        for w in words:
            f.write(w + "\n")

# Глобальный список (загружается при старте)
STOPWORDS = load_stopwords()

def contains_spam(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    for word in STOPWORDS:
        if word in text_lower:
            return True
    return False

def add_stopword(word: str):
    w = word.strip().lower()
    if w and w not in STOPWORDS:
        STOPWORDS.append(w)
        save_stopwords(STOPWORDS)

def remove_stopword(word: str):
    w = word.strip().lower()
    if w in STOPWORDS:
        STOPWORDS.remove(w)
        save_stopwords(STOPWORDS)