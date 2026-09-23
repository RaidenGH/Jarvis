import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as status;

import 'config.dart';
import 'transcript.dart';

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  late final TextEditingController _input;
  final ScrollController _scroll = ScrollController();
  final Transcript _transcript = Transcript();

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  bool _connected = false;
  bool _assistantTyping = false;
  bool _speaking = false;
  bool _confirming = false; // approval dialog is up for a gated tool call
  String _pttState = 'idle'; // idle | recording | transcribing
  bool _listening = false; // always-listening (wake word) armed
  String _wakeWord = 'hey jarvis';

  /// Whether new content should pull the view along. Goes false the moment
  /// you scroll up to read something, so a chatty reply can't yank the page.
  bool _followTail = true;

  @override
  void initState() {
    super.initState();
    _input = TextEditingController();
    _scroll.addListener(_onScroll);
    _connect();
  }

  @override
  void dispose() {
    _sub?.cancel();
    _channel?.sink.close(status.normalClosure);
    _scroll.removeListener(_onScroll);
    _scroll.dispose();
    _input.dispose();
    super.dispose();
  }

  // --- connection ---------------------------------------------------------

  void _connect() {
    _sub?.cancel();
    _channel?.sink.close(status.normalClosure);
    final channel = WebSocketChannel.connect(Uri.parse(kDefaultWsUrl));
    setState(() => _channel = channel);
    channel.ready.then((_) {
      if (mounted) setState(() => _connected = true);
    }).catchError((_) {
      if (mounted) setState(() => _connected = false);
    });
    _sub = channel.stream.listen(
      (data) => _handleEvent(data as String),
      onError: (_) {
        if (mounted) setState(() => _connected = false);
      },
      onDone: () {
        if (mounted) setState(() => _connected = false);
      },
      cancelOnError: false,
    );
  }

  void _onScroll() {
    if (!_scroll.hasClients) return;
    final position = _scroll.position;
    _followTail = position.pixels >= position.maxScrollExtent - 120;
  }

  void _scrollToEnd() {
    if (!_followTail) return;
    // After the frame, so maxScrollExtent already accounts for the new row.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 220),
        curve: Curves.easeOutCubic,
      );
    });
  }

  // --- protocol -----------------------------------------------------------

  void _handleEvent(String raw) {
    final Object? decoded;
    try {
      decoded = jsonDecode(raw);
    } catch (_) {
      return; // ignore malformed frames
    }
    if (decoded is! Map<String, dynamic>) return;
    final map = decoded; // promote once; closures don't see the is!-promotion

    switch (map['type']) {
      case 'token':
        setState(() => _transcript.appendAssistantToken((map['text'] ?? '') as String));
        _scrollToEnd();
      case 'tool_call':
        setState(() {
          _transcript.startToolCall(
            name: (map['name'] ?? '?') as String,
            arguments:
                (map['arguments'] as Map?)?.cast<String, dynamic>() ?? const {},
            id: map['id'] as String?,
          );
        });
        _scrollToEnd();
      case 'tool_result':
        setState(() => _transcript.finishTool(
              (map['name'] ?? '?') as String,
              map['result'],
            ));
      case 'tool_denied':
        setState(() => _transcript.toolDenied(
              (map['name'] ?? '?') as String,
              status: map['decision'] == 'disabled'
                  ? ToolStatus.disabled
                  : ToolStatus.denied,
              reason: map['reason'] as String?,
              risk: map['risk'] as String?,
            ));
      case 'confirm_request':
        setState(() => _transcript.toolAwaitingApproval(
              (map['name'] ?? '?') as String,
              risk: map['risk'] as String?,
              mode: map['mode'] as String?,
            ));
        _askConfirmation(map);
      case 'done':
        setState(() {
          _assistantTyping = false;
          _speaking = false;
          _confirming = false;
          _pttState = 'idle';
        });
      case 'reset_done':
        setState(() {
          _transcript.clear();
          _pttState = 'idle';
        });
      case 'transcript':
        setState(() {
          _transcript.addUser((map['text'] ?? '') as String);
          _assistantTyping = true;
        });
        _scrollToEnd();
      case 'ptt_state':
        final st = (map['state'] ?? 'idle') as String;
        setState(() {
          _pttState = st;
          if (st == 'transcribing') _assistantTyping = true;
          if (st == 'recording') _assistantTyping = false;
        });
      case 'listen_state':
        final st = (map['state'] ?? 'idle') as String;
        setState(() {
          _listening = st != 'idle';
          if (st == 'capturing') _assistantTyping = true;
        });
      case 'wake_detected':
        setState(() {
          final name = ((map['name'] ?? '') as String).replaceAll('_', ' ');
          if (name.isNotEmpty) _wakeWord = name;
          _assistantTyping = true;
        });
      case 'tts_start':
        setState(() => _speaking = true);
      case 'tts_done':
        setState(() => _speaking = false);
      case 'error':
        setState(() => _assistantTyping = false);
        _showError((map['message'] ?? 'error') as String);
      default:
        break;
    }
  }

  void _showError(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  // --- sending ------------------------------------------------------------

  void _submit(String raw, {bool clearInput = false}) {
    final text = raw.trim();
    if (text.isEmpty || !_connected || _assistantTyping) return;
    setState(() {
      _transcript.addUser(text);
      _assistantTyping = true;
      if (clearInput) _input.clear();
    });
    _followTail = true;
    _channel!.sink.add(jsonEncode({'type': 'user_message', 'text': text}));
    _scrollToEnd();
  }

  void _send() => _submit(_input.text, clearInput: true);

  void _sendPtt(String type) {
    if (!_connected) return;
    _channel!.sink.add(jsonEncode({'type': type}));
    if (type == 'ptt_start') {
      setState(() => _pttState = 'recording');
    }
  }

  void _reset() {
    if (_connected) {
      _channel!.sink.add(jsonEncode({'type': 'reset'}));
    } else {
      setState(_transcript.clear);
    }
  }

  void _reconnect() {
    setState(_transcript.clear);
    _connect();
  }

  void _toggleListening() {
    if (!_connected) return;
    if (_listening) {
      _channel!.sink.add(jsonEncode({'type': 'listen_stop'}));
    } else {
      if (_assistantTyping || _speaking) return;
      _channel!.sink.add(jsonEncode({'type': 'listen_start'}));
    }
  }

  /// Ask the human to approve a gated tool call, then answer the backend.
  ///
  /// The backend enforces this too — a typed tier is only satisfied by an
  /// exact echo of its challenge phrase — so matching the text here just
  /// keeps the Allow button honest, it isn't the security boundary.
  Future<void> _askConfirmation(Map<String, dynamic> request) async {
    if (!mounted) return;
    final name = (request['name'] ?? 'action') as String;
    final risk = (request['risk'] ?? 'unknown') as String;
    final mode = (request['mode'] ?? 'tap') as String;
    final challenge = (request['challenge'] ?? '') as String;
    final args = jsonEncode(request['arguments'] ?? const <String, dynamic>{});
    final typed = TextEditingController();
    setState(() => _confirming = true);

    bool ok = false;
    String typedText = '';
    try {
      final approved = await showDialog<bool>(
        context: context,
        barrierDismissible: false,
        builder: (ctx) => StatefulBuilder(
          builder: (ctx, setDialogState) {
            final matches = typed.text.trim() == challenge;
            return AlertDialog(
              icon: Icon(
                mode == 'typed' ? Icons.warning_amber_rounded : Icons.shield_outlined,
              ),
              title: const Text('Allow this action?'),
              content: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: Theme.of(ctx).colorScheme.surfaceContainerHighest,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: SelectableText(
                      '$name($args)',
                      style: const TextStyle(fontFamily: 'monospace', fontSize: 12),
                    ),
                  ),
                  const SizedBox(height: 10),
                  Text('Risk tier: $risk',
                      style: Theme.of(ctx).textTheme.bodySmall),
                  if (mode == 'typed') ...[
                    const SizedBox(height: 12),
                    Text('Type "$challenge" to confirm:'),
                    TextField(
                      controller: typed,
                      autofocus: true,
                      onChanged: (_) => setDialogState(() {}),
                      decoration: const InputDecoration(isDense: true),
                    ),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(ctx).pop(false),
                  child: const Text('Deny'),
                ),
                FilledButton(
                  onPressed: (mode == 'typed' && !matches)
                      ? null
                      : () => Navigator.of(ctx).pop(true),
                  child: const Text('Allow'),
                ),
              ],
            );
          },
        ),
      );
      ok = approved ?? false;
      typedText = typed.text.trim();
    } finally {
      typed.dispose();
      if (mounted) setState(() => _confirming = false);
    }

    _channel?.sink.add(jsonEncode({
      'type': 'confirm_response',
      'approved': ok,
      'challenge': mode == 'typed' ? typedText : null,
    }));
  }

  // --- derived state ------------------------------------------------------

  _Status? get _status {
    if (!_connected) return null;
    if (_confirming) {
      return const _Status('Waiting for your approval…', alert: true);
    }
    if (_pttState == 'recording') {
      return const _Status('Listening — release when done', pulse: true, alert: true);
    }
    if (_pttState == 'transcribing') {
      return const _Status('Transcribing…', spinner: true);
    }
    if (_speaking) return const _Status('Speaking…', spinner: true);
    if (_listening) {
      return _Status('Listening for "$_wakeWord"', pulse: true);
    }
    return null;
  }

  @override
  Widget build(BuildContext context) {
    final status = _status;
    final busy = _assistantTyping || _speaking || _confirming;

    return Scaffold(
      appBar: AppBar(
        leadingWidth: 44,
        leading: Center(child: _StatusDot(connected: _connected)),
        title: const Text('Jarvis'),
        actions: [
          IconButton(
            isSelected: _listening,
            icon: const Icon(Icons.hearing_disabled),
            selectedIcon: const Icon(Icons.hearing),
            tooltip: _listening
                ? 'Stop always-listening'
                : 'Always listen for the wake word',
            onPressed: _connected ? _toggleListening : null,
          ),
          PopupMenuButton<String>(
            tooltip: 'More',
            onSelected: (value) {
              if (value == 'new') _reset();
              if (value == 'reconnect') _reconnect();
            },
            itemBuilder: (_) => const [
              PopupMenuItem(value: 'new', child: Text('New session')),
              PopupMenuItem(value: 'reconnect', child: Text('Reconnect')),
            ],
          ),
        ],
      ),
      body: Column(
        children: [
          if (!_connected) const _OfflineBanner(),
          _StatusBar(status: status, recording: _pttState == 'recording'),
          Expanded(
            child: _transcript.isEmpty
                ? _EmptyState(
                    connected: _connected,
                    wakeWord: _wakeWord,
                    onSuggestion: (text) => _submit(text),
                  )
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(16, 12, 16, 16),
                    itemCount: _transcript.length,
                    itemBuilder: (context, i) {
                      final entry = _transcript.entries[i];
                      final maxWidth =
                          (MediaQuery.sizeOf(context).width * 0.82).clamp(0.0, 680.0);
                      return _Enter(
                        key: ObjectKey(entry),
                        child: switch (entry) {
                          UserEntry e => _Bubble(
                              text: e.text,
                              isUser: true,
                              maxWidth: maxWidth,
                            ),
                          AssistantEntry e => _Bubble(
                              text: e.text,
                              isUser: false,
                              maxWidth: maxWidth,
                            ),
                          ToolEntry e => _ToolChip(entry: e),
                        },
                      );
                    },
                  ),
          ),
          _Composer(
            controller: _input,
            connected: _connected,
            busy: busy,
            recording: _pttState == 'recording',
            listening: _listening,
            wakeWord: _wakeWord,
            speaking: _speaking,
            onSend: _send,
            onPttStart: () => _sendPtt('ptt_start'),
            onPttStop: () => _sendPtt('ptt_stop'),
          ),
        ],
      ),
    );
  }
}

/// What the assistant is doing right now, if anything.
class _Status {
  const _Status(
    this.text, {
    this.spinner = false,
    this.pulse = false,
    this.alert = false,
  });

  final String text;
  final bool spinner;
  final bool pulse;
  final bool alert;
}

class _StatusBar extends StatelessWidget {
  const _StatusBar({required this.status, required this.recording});

  final _Status? status;
  final bool recording;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final status = this.status;
    return AnimatedSize(
      duration: const Duration(milliseconds: 180),
      curve: Curves.easeOut,
      alignment: Alignment.topCenter,
      child: status == null
          ? const SizedBox(width: double.infinity)
          : Container(
              width: double.infinity,
              color: status.alert
                  ? theme.colorScheme.errorContainer
                  : theme.colorScheme.secondaryContainer,
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
              child: Row(
                children: [
                  _StatusIndicator(status: status),
                  const SizedBox(width: 10),
                  Text(
                    status.text,
                    style: theme.textTheme.labelLarge?.copyWith(
                      color: status.alert
                          ? theme.colorScheme.onErrorContainer
                          : theme.colorScheme.onSecondaryContainer,
                    ),
                  ),
                ],
              ),
            ),
    );
  }
}

class _StatusIndicator extends StatelessWidget {
  const _StatusIndicator({required this.status});

  final _Status status;

  @override
  Widget build(BuildContext context) {
    if (status.spinner) {
      return const SizedBox(
        width: 14,
        height: 14,
        child: CircularProgressIndicator(strokeWidth: 2),
      );
    }
    if (status.pulse) return const _PulseDot();
    return Icon(Icons.shield_outlined,
        size: 16, color: Theme.of(context).colorScheme.onErrorContainer);
  }
}

/// A slow breathing dot — used for "the mic is live", which should be
/// visible and honest rather than an invisible state (plan §4.2).
class _PulseDot extends StatefulWidget {
  const _PulseDot();

  @override
  State<_PulseDot> createState() => _PulseDotState();
}

class _PulseDotState extends State<_PulseDot>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 900),
  )..repeat(reverse: true);

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return FadeTransition(
      opacity: Tween<double>(begin: 0.35, end: 1).animate(
        CurvedAnimation(parent: _controller, curve: Curves.easeInOut),
      ),
      child: Container(
        width: 10,
        height: 10,
        decoration: BoxDecoration(
          color: theme.colorScheme.error,
          shape: BoxShape.circle,
        ),
      ),
    );
  }
}

class _StatusDot extends StatelessWidget {
  const _StatusDot({required this.connected});

  final bool connected;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Tooltip(
      message: connected ? 'Connected to the backend' : 'Not connected',
      child: Container(
        width: 12,
        height: 12,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: connected
              ? theme.colorScheme.primary
              : theme.colorScheme.error,
          boxShadow: [
            BoxShadow(
              color: (connected
                      ? theme.colorScheme.primary
                      : theme.colorScheme.error)
                  .withValues(alpha: 0.35),
              blurRadius: 8,
              spreadRadius: 1,
            ),
          ],
        ),
      ),
    );
  }
}

class _OfflineBanner extends StatelessWidget {
  const _OfflineBanner();

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      color: theme.colorScheme.errorContainer,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(
        children: [
          Icon(Icons.cloud_off,
              size: 16, color: theme.colorScheme.onErrorContainer),
          const SizedBox(width: 10),
          Text(
            'Backend offline — start it, then reconnect',
            style: theme.textTheme.labelLarge
                ?.copyWith(color: theme.colorScheme.onErrorContainer),
          ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({
    required this.connected,
    required this.wakeWord,
    required this.onSuggestion,
  });

  final bool connected;
  final String wakeWord;
  final ValueChanged<String> onSuggestion;

  static const _suggestions = [
    'What are my system stats?',
    'Find the test files',
    'Open notepad',
  ];

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(32),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(
                color: theme.colorScheme.secondaryContainer,
                shape: BoxShape.circle,
              ),
              child: Icon(
                Icons.graphic_eq,
                size: 32,
                color: theme.colorScheme.onSecondaryContainer,
              ),
            ),
            const SizedBox(height: 20),
            Text('Ask me anything', style: theme.textTheme.titleMedium),
            const SizedBox(height: 6),
            Text(
              'Type below, hold the mic to talk, or tap the ear to wake me '
              'with "$wakeWord".',
              textAlign: TextAlign.center,
              style: theme.textTheme.bodyMedium
                  ?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
            if (connected) ...[
              const SizedBox(height: 24),
              Wrap(
                alignment: WrapAlignment.center,
                spacing: 8,
                runSpacing: 8,
                children: [
                  for (final suggestion in _suggestions)
                    ActionChip(
                      label: Text(suggestion),
                      onPressed: () => onSuggestion(suggestion),
                    ),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.text,
    required this.isUser,
    required this.maxWidth,
  });

  final String text;
  final bool isUser;
  final double maxWidth;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    // The far corner is pinched, which is what reads as a "tail" without
    // drawing one.
    final radius = BorderRadius.only(
      topLeft: const Radius.circular(18),
      topRight: const Radius.circular(18),
      bottomLeft: Radius.circular(isUser ? 18 : 4),
      bottomRight: Radius.circular(isUser ? 4 : 18),
    );
    return Align(
      alignment: isUser ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        constraints: BoxConstraints(maxWidth: maxWidth),
        margin: EdgeInsets.only(
          top: 3,
          bottom: 3,
          left: isUser ? 48 : 0,
          right: isUser ? 0 : 48,
        ),
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
        decoration: BoxDecoration(
          color: isUser
              ? theme.colorScheme.primary
              : theme.colorScheme.surfaceContainerHigh,
          borderRadius: radius,
        ),
        child: SelectableText(
          text,
          style: theme.textTheme.bodyLarge?.copyWith(
            color: isUser
                ? theme.colorScheme.onPrimary
                : theme.colorScheme.onSurface,
          ),
        ),
      ),
    );
  }
}

/// One tool call as a compact row: name, arguments, status. Tap to see the
/// result. Declined and disabled calls are shown too — a blocked action is
/// information, not something to hide.
class _ToolChip extends StatefulWidget {
  const _ToolChip({required this.entry});

  final ToolEntry entry;

  @override
  State<_ToolChip> createState() => _ToolChipState();
}

class _ToolChipState extends State<_ToolChip> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final entry = widget.entry;
    final blocked = entry.status == ToolStatus.denied ||
        entry.status == ToolStatus.disabled;

    return Align(
      alignment: Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 2),
        constraints: const BoxConstraints(maxWidth: 620),
        decoration: BoxDecoration(
          color: theme.colorScheme.surfaceContainer,
          borderRadius: BorderRadius.circular(12),
          border: Border.all(
            color: blocked
                ? theme.colorScheme.error.withValues(alpha: 0.5)
                : theme.colorScheme.outlineVariant.withValues(alpha: 0.6),
          ),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            InkWell(
              borderRadius: BorderRadius.circular(12),
              onTap: entry.hasDetail
                  ? () => setState(() => _expanded = !_expanded)
                  : null,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(10, 8, 8, 8),
                child: Row(
                  children: [
                    _ToolStatusIcon(status: entry.status),
                    const SizedBox(width: 8),
                    Text(
                      entry.name,
                      style: TextStyle(
                        fontFamily: 'monospace',
                        fontSize: 12.5,
                        fontWeight: FontWeight.w600,
                        color: blocked
                            ? theme.colorScheme.error
                            : theme.colorScheme.onSurface,
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        entry.argumentSummary,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: theme.textTheme.bodySmall?.copyWith(
                          color: theme.colorScheme.onSurfaceVariant,
                          fontFamily: 'monospace',
                          fontSize: 11.5,
                        ),
                      ),
                    ),
                    if (entry.hasDetail)
                      Icon(
                        _expanded ? Icons.expand_less : Icons.expand_more,
                        size: 16,
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                  ],
                ),
              ),
            ),
            if (_expanded && entry.hasDetail)
              Container(
                width: double.infinity,
                margin: const EdgeInsets.fromLTRB(10, 0, 10, 10),
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: theme.colorScheme.surfaceContainerLowest,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: SelectableText(
                  entry.detail!,
                  style: TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 11.5,
                    height: 1.4,
                    color: blocked
                        ? theme.colorScheme.error
                        : theme.colorScheme.onSurfaceVariant,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class _ToolStatusIcon extends StatelessWidget {
  const _ToolStatusIcon({required this.status});

  final ToolStatus status;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    switch (status) {
      case ToolStatus.running:
        return const SizedBox(
          width: 14,
          height: 14,
          child: CircularProgressIndicator(strokeWidth: 2),
        );
      case ToolStatus.awaiting:
        return Icon(Icons.pending_outlined,
            size: 16, color: theme.colorScheme.primary);
      case ToolStatus.done:
        return Icon(Icons.check_circle_outline,
            size: 16, color: theme.colorScheme.primary);
      case ToolStatus.denied:
        return Icon(Icons.block, size: 16, color: theme.colorScheme.error);
      case ToolStatus.disabled:
        return Icon(Icons.lock_outline, size: 16, color: theme.colorScheme.error);
    }
  }
}

/// The composer: a single pill holding hold-to-talk, the field, and send.
class _Composer extends StatelessWidget {
  const _Composer({
    required this.controller,
    required this.connected,
    required this.busy,
    required this.recording,
    required this.listening,
    required this.wakeWord,
    required this.speaking,
    required this.onSend,
    required this.onPttStart,
    required this.onPttStop,
  });

  final TextEditingController controller;
  final bool connected;
  final bool busy;
  final bool recording;
  final bool listening;
  final String wakeWord;
  final bool speaking;
  final VoidCallback onSend;
  final VoidCallback onPttStart;
  final VoidCallback onPttStop;

  String get _hint {
    if (!connected) return 'Offline';
    if (recording) return 'Listening…';
    if (busy) return speaking ? 'Speaking…' : 'Thinking…';
    if (listening) return 'Listening for "$wakeWord"…';
    return 'Message Jarvis';
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final canSend = connected && !busy;
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
          decoration: BoxDecoration(
            color: theme.colorScheme.surfaceContainerHigh,
            borderRadius: BorderRadius.circular(28),
            border: Border.all(
              color: theme.colorScheme.outlineVariant.withValues(alpha: 0.5),
            ),
          ),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.center,
            children: [
              _HoldToTalkButton(
                recording: recording,
                enabled: connected && !busy,
                onDown: onPttStart,
                onUp: onPttStop,
                onCancel: onPttStop,
              ),
              const SizedBox(width: 4),
              Expanded(
                child: TextField(
                  controller: controller,
                  enabled: connected && !recording,
                  textInputAction: TextInputAction.send,
                  onSubmitted: (_) => onSend(),
                  style: theme.textTheme.bodyLarge,
                  decoration: InputDecoration(
                    hintText: _hint,
                    border: InputBorder.none,
                    enabledBorder: InputBorder.none,
                    focusedBorder: InputBorder.none,
                    isDense: true,
                    contentPadding: const EdgeInsets.symmetric(vertical: 12),
                  ),
                ),
              ),
              const SizedBox(width: 4),
              IconButton.filled(
                icon: const Icon(Icons.arrow_upward, size: 20),
                tooltip: 'Send',
                onPressed: canSend ? onSend : null,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// Hold to talk: press fires ptt_start, release fires ptt_stop.
class _HoldToTalkButton extends StatelessWidget {
  const _HoldToTalkButton({
    required this.recording,
    required this.enabled,
    required this.onDown,
    required this.onUp,
    required this.onCancel,
  });

  final bool recording;
  final bool enabled;
  final VoidCallback onDown;
  final VoidCallback onUp;
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final Color background;
    final Color foreground;
    if (!enabled && !recording) {
      background = theme.colorScheme.surfaceContainerHighest;
      foreground = theme.colorScheme.onSurfaceVariant.withValues(alpha: 0.5);
    } else if (recording) {
      background = theme.colorScheme.error;
      foreground = theme.colorScheme.onError;
    } else {
      background = theme.colorScheme.secondaryContainer;
      foreground = theme.colorScheme.onSecondaryContainer;
    }

    return Tooltip(
      message: recording ? 'Release to send' : 'Hold to talk',
      child: Material(
        color: background,
        shape: const CircleBorder(),
        child: InkWell(
          customBorder: const CircleBorder(),
          onTapDown: enabled ? (_) => onDown() : null,
          onTapUp: enabled ? (_) => onUp() : null,
          onTapCancel: enabled ? onCancel : null,
          child: SizedBox(
            width: 40,
            height: 40,
            child: Icon(
              recording ? Icons.stop_rounded : Icons.mic_none_rounded,
              size: 20,
              color: foreground,
            ),
          ),
        ),
      ),
    );
  }
}

/// Fades and lifts a row into place the first time it appears.
class _Enter extends StatefulWidget {
  const _Enter({required this.child, super.key});

  final Widget child;

  @override
  State<_Enter> createState() => _EnterState();
}

class _EnterState extends State<_Enter> with SingleTickerProviderStateMixin {
  late final AnimationController _controller = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 220),
  )..forward();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final curve = CurvedAnimation(parent: _controller, curve: Curves.easeOutCubic);
    return FadeTransition(
      opacity: curve,
      child: SlideTransition(
        position: Tween<Offset>(
          begin: const Offset(0, 0.06),
          end: Offset.zero,
        ).animate(curve),
        child: widget.child,
      ),
    );
  }
}
