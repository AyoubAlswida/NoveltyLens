from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


def compare_invention_to_patents(invention_text: str, patents: list):
    """
    Returns similarity score for each patent.
    """

    if not patents:
        return []

    docs = [invention_text]

    for p in patents:
        title = str(p.get("title", ""))
        abstract = str(p.get("abstract", "") or "")
        text = f"{title} {abstract}".strip()
        docs.append(text)

    vectorizer = TfidfVectorizer(stop_words="english")
    tfidf = vectorizer.fit_transform(docs)

    invention_vec = tfidf[0]
    patent_vecs = tfidf[1:]

    scores = cosine_similarity(invention_vec, patent_vecs)[0]

    results = []

    for i, score in enumerate(scores):
        results.append({
            "patent": patents[i],
            "similarity_score": float(score)
        })

    results.sort(key=lambda x: x["similarity_score"], reverse=True)

    return results