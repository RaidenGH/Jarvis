import 'dart:convert';

/// How a tool call ended up.
enum ToolStatus {
  /// Requested, not finished yet.
  running,

  /// Paused, waiting for the human to approve it.
  awaiting,

  /// Ran and returned a result.
  done,

  /// The human declined it.
  denied,

  /// Its risk tier is disabled, so it was never allowed to run.
  disabled,
}

/// One row in the transcript.
sealed class ChatEntry {
  const ChatEntry();
}

/// Something the user said or typed.
final class UserEntry extends ChatEntry {
  UserEntry(this.text);

  final String text;
}

/// Something the assistant said. Mutable because replies arrive as tokens.
final class AssistantEntry extends ChatEntry {
  AssistantEntry([this.text = '']);

  String text;
}

/// A tool the brain wanted to call, and what came of it.
///
/// Kept in the transcript rather than hidden, so the permission gate is
/// visible: you can see what was asked for, what a call returned, and what
/// you declined.
final class ToolEntry extends ChatEntry {
  ToolEntry({required this.name, this.arguments = const {}, this.id});

  final String? id;
  final String name;
  final Map<String, dynamic> arguments;

  ToolStatus status = ToolStatus.running;
  String? risk;
  String? mode;

  /// Result JSON, or the reason it didn't run.
  String? detail;

  bool get hasDetail => detail != null && detail!.isNotEmpty;

  /// A one-line gist of the arguments, for the collapsed row.
  String get argumentSummary {
    if (arguments.isEmpty) return 'no arguments';
    return arguments.entries
        .map((e) => '${e.key}: ${_shortValue(e.value)}')
        .join(', ');
  }
}

const int _maxDetailChars = 4000;
const int _maxSummaryValueChars = 28;

String _shortValue(Object? value) {
  final text = value is String ? value : jsonEncode(value);
  if (text.length <= _maxSummaryValueChars) return text;
  return '${text.substring(0, _maxSummaryValueChars - 1)}…';
}

/// Pretty-print a tool result for the expanded view, capped so one huge
/// result can't blow up the widget tree.
String previewOf(Object? value) {
  String text;
  try {
    text = const JsonEncoder.withIndent('  ').convert(value);
  } catch (_) {
    text = value?.toString() ?? '';
  }
  if (text.length > _maxDetailChars) {
    return '${text.substring(0, _maxDetailChars)}\n… truncated (${text.length} chars)';
  }
  return text;
}

/// The conversation as the UI needs it: ordered, mutable, protocol-aware.
///
/// Deliberately free of Flutter imports so the frame-to-entry rules (which
/// bubble a token belongs to, how a tool call pairs with its result) can be
/// unit tested without a WebSocket or a widget tree.
class Transcript {
  final List<ChatEntry> entries = [];

  bool get isEmpty => entries.isEmpty;
  int get length => entries.length;

  void clear() => entries.clear();

  void addUser(String text) => entries.add(UserEntry(text));

  /// Append a reply token, opening a new assistant bubble unless the
  /// assistant's is already the last thing in the transcript.
  ///
  /// This is the rule that broke once: keying off a "busy" flag meant the
  /// reply was appended to the user's own message instead of its own bubble.
  void appendAssistantToken(String token) {
    final last = entries.isEmpty ? null : entries.last;
    final target = last is AssistantEntry ? last : _openAssistant();
    target.text += token;
  }

  AssistantEntry _openAssistant() {
    final entry = AssistantEntry();
    entries.add(entry);
    return entry;
  }

  /// A `tool_call` frame arrived: show the attempt, mark it in flight.
  ToolEntry startToolCall({
    required String name,
    Map<String, dynamic> arguments = const {},
    String? id,
  }) {
    final entry = ToolEntry(name: name, arguments: arguments, id: id);
    entries.add(entry);
    return entry;
  }

  /// A `confirm_request` frame arrived: the gate is asking about this call.
  void toolAwaitingApproval(String name, {String? risk, String? mode}) {
    final entry = _pending(name) ?? startToolCall(name: name);
    entry.status = ToolStatus.awaiting;
    entry.risk ??= risk;
    entry.mode ??= mode;
  }

  /// A `tool_result` frame arrived.
  void finishTool(String name, Object? result) {
    final entry = _pending(name) ?? startToolCall(name: name);
    entry.status = ToolStatus.done;
    entry.detail = previewOf(result);
  }

  /// A `tool_denied` frame arrived — declined by the user, or disabled.
  void toolDenied(
    String name, {
    ToolStatus status = ToolStatus.denied,
    String? reason,
    String? risk,
  }) {
    final entry = _pending(name) ?? startToolCall(name: name);
    entry.status = status;
    entry.detail = reason;
    entry.risk ??= risk;
  }

  /// The most recent call for `name` that is still unresolved.
  ToolEntry? _pending(String name) {
    for (final entry in entries.reversed) {
      if (entry is ToolEntry &&
          entry.name == name &&
          (entry.status == ToolStatus.running ||
              entry.status == ToolStatus.awaiting)) {
        return entry;
      }
    }
    return null;
  }
}
