/** Keep a publication and its comparison together in one report bullet. */
export function recommendationBullets(recommendations: readonly string[]): string[] {
  const bullets: string[] = [];
  const publicationPositions = new Map<string, number>();
  for (const recommendation of recommendations) {
    const text = recommendation
      .replace(/(?:^|\s+)0 additional screened candidates are included for further review\.(?=\s*$)/, '')
      .split(/\r?\n|\s+(?=\d+[.)]\s|[-•]\s)/u)
      .map((point) => point.trim().replace(/^(?:\d+[.)]|[-•])\s+/, '').replace(/\*\*/g, ''))
      .filter(Boolean)
      .join(' ');
    if (!text) continue;
    const reference = /^([A-Z]{2}\d+[A-Z]\d*)\s*(?:[.—:]\s+)(.*)$/u.exec(text);
    if (!reference) {
      bullets.push(text.replace(/\s*—\s*/g, ', '));
      continue;
    }
    const [, publication, comparison] = reference;
    const prose = comparison.replace(/\s*—\s*/g, ', ');
    const position = publicationPositions.get(publication);
    if (position === undefined) {
      publicationPositions.set(publication, bullets.length);
      bullets.push(`${publication}. ${prose}`);
    } else {
      bullets[position] += ` ${prose}`;
    }
  }
  return bullets;
}
