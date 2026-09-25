/// One entry in the command palette.
///
/// Deliberately a plain data class with no Flutter types: the ranking rules
/// are the sort of thing that quietly rots, so they're testable on their own
/// — same split as `transcript.dart`.
class PaletteAction {
  const PaletteAction({
    required this.id,
    required this.label,
    this.detail = '',
    this.keywords = const [],
    this.prompt = false,
  });

  final String id;
  final String label;
  final String detail;

  /// Extra words that should match but aren't shown, e.g. "wake" for
  /// "Always-listening".
  final List<String> keywords;

  /// True when picking this sends a message rather than running a command.
  final bool prompt;
}

/// Rank [actions] against [query], best first. An empty query keeps the
/// authored order — that's the "here's everything" list.
List<PaletteAction> rankActions(List<PaletteAction> actions, String query) {
  final q = query.trim().toLowerCase();
  if (q.isEmpty) return List.of(actions);

  final scored = <(int score, int index, PaletteAction action)>[];
  for (var i = 0; i < actions.length; i++) {
    final action = actions[i];
    final label = action.label.toLowerCase();
    final direct = label.indexOf(q);

    final int? score;
    if (direct >= 0) {
      // Substring hits beat fuzzy ones, and earlier/shorter labels beat later.
      score = 400 - direct * 6 - label.length;
    } else {
      final haystack = [
        label,
        action.detail.toLowerCase(),
        for (final k in action.keywords) k.toLowerCase(),
      ].join(' ');
      score = _fuzzy(label, q) ?? _fuzzy(haystack, q);
    }
    if (score != null) scored.add((score, i, action));
  }

  scored.sort((a, b) {
    final byScore = b.$1.compareTo(a.$1);
    return byScore != 0 ? byScore : a.$2.compareTo(b.$2);
  });
  return [for (final row in scored) row.$3];
}

/// Subsequence match with a bias toward contiguous runs (`npm` → "new
/// session" is out, `ns` → "New session" is in) and word starts.
int? _fuzzy(String haystack, String needle) {
  if (needle.isEmpty) return 0;
  var cursor = 0;
  var score = 0;
  var streak = 0;

  for (var i = 0; i < needle.length; i++) {
    final found = haystack.indexOf(needle[i], cursor);
    if (found < 0) return null;
    if (streak > 0 && found == cursor) {
      streak++;
      score += 8 + streak;
    } else {
      streak = 1;
      score += 4;
    }
    if (found == 0 || haystack[found - 1] == ' ') score += 6;
    cursor = found + 1;
  }
  // Prefer tighter matches: penalise whatever was left unmatched.
  return score - (haystack.length - cursor) ~/ 3;
}
