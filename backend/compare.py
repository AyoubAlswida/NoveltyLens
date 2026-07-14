"""Patent ranking.  This is the only module coupled to the ranking method."""

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from models import ComparisonResult, PatentRecord


def compare_invention_to_patents(
    invention_text: str, patents: list[PatentRecord]
) -> list[ComparisonResult]:
    """Rank normalized patents by TF-IDF cosine similarity.

    The algorithm intentionally remains unchanged so a future embedding-based
    implementation can replace this module without affecting callers.
    """
    if not patents:
        return []

    documents = [invention_text]
    documents.extend(f"{patent.title} {patent.abstract}".strip() for patent in patents)

    vectorizer = TfidfVectorizer(stop_words="english")
    tfidf = vectorizer.fit_transform(documents)
    scores = cosine_similarity(tfidf[0], tfidf[1:])[0]

    return sorted(
        (
            ComparisonResult(patent=patent, similarity_score=float(score))
            for patent, score in zip(patents, scores, strict=True)
        ),
        key=lambda result: result.similarity_score,
        reverse=True,
    )
