import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:web_socket_channel/status.dart' as status;

import 'config.dart';

class ChatMessage {
  ChatMessage({required this.role, required this.text});
  final String role; // "user" | "assistant"
  String text;
}

class ChatScreen extends StatefulWidget {
  const ChatScreen({super.key});

  @override
  State<ChatScreen> createState() => _ChatScreenState();
}

class _ChatScreenState extends State<ChatScreen> {
  late final TextEditingController _input;
  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  final List<ChatMessage> _messages = [];
  bool _connected = false;
  bool _assistantTyping = false;
  bool _speaking = false;
  String _pttState = 'idle'; // idle | recording | transcribing

  @override
  void initState() {
    super.initState();
    _input = TextEditingController();
    _connect();
  }

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
        setState(() {
          if (!_assistantTyping) {
            _messages.add(ChatMessage(role: 'assistant', text: ''));
            _assistantTyping = true;
          }
          _messages.last.text += (map['text'] ?? '') as String;
        });
      case 'done':
        setState(() {
          _assistantTyping = false;
          _speaking = false;
          _pttState = 'idle';
        });
      case 'reset_done':
        setState(() {
          _messages.clear();
          _pttState = 'idle';
        });
      case 'transcript':
        setState(() {
          _messages.add(
              ChatMessage(role: 'user', text: (map['text'] ?? '') as String));
          _assistantTyping = true;
        });
      case 'ptt_state':
        final st = (map['state'] ?? 'idle') as String;
        setState(() {
          _pttState = st;
          if (st == 'transcribing') _assistantTyping = true;
          if (st == 'recording') _assistantTyping = false;
        });
      case 'tts_start':
        setState(() => _speaking = true);
      case 'tts_done':
        setState(() => _speaking = false);
      case 'error':
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

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty || !_connected || _assistantTyping) return;
    setState(() {
      _messages.add(ChatMessage(role: 'user', text: text));
      _assistantTyping = true;
      _input.clear();
    });
    _channel!.sink.add(jsonEncode({'type': 'user_message', 'text': text}));
  }

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
      setState(_messages.clear);
    }
  }

  String? get _statusLine {
    if (_pttState == 'recording') return 'Listening… release when done';
    if (_pttState == 'transcribing') return 'Transcribing…';
    if (_speaking) return 'Speaking…';
    return null;
  }

  @override
  void dispose() {
    _sub?.cancel();
    _channel?.sink.close(status.normalClosure);
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final statusLine = _statusLine;
    final busy = _assistantTyping || _speaking;
    return Scaffold(
      appBar: AppBar(
        title: Row(children: [
          Icon(Icons.circle,
              size: 12, color: _connected ? Colors.greenAccent : Colors.redAccent),
          const SizedBox(width: 8),
          const Text('Jarvis'),
        ]),
        actions: [
          IconButton(
              icon: const Icon(Icons.refresh),
              tooltip: 'New session',
              onPressed: _reset),
          IconButton(
              icon: const Icon(Icons.link),
              tooltip: 'Reconnect',
              onPressed: () {
                setState(_messages.clear);
                _connect();
              }),
        ],
      ),
      body: Column(children: [
        if (statusLine != null)
          Container(
            width: double.infinity,
            color: _pttState == 'recording'
                ? theme.colorScheme.errorContainer
                : theme.colorScheme.tertiaryContainer,
            padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 12),
            child: Row(children: [
              const SizedBox(
                width: 12,
                height: 12,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              const SizedBox(width: 10),
              Text(statusLine, style: theme.textTheme.bodyMedium),
            ]),
          ),
        Expanded(
          child: _messages.isEmpty
              ? Center(
                  child: Text('Phase 1: type or hold 🎤 to talk',
                      style: theme.textTheme.bodySmall))
              : ListView.builder(
                  padding: const EdgeInsets.all(12),
                  itemCount: _messages.length,
                  itemBuilder: (_, i) {
                    final m = _messages[i];
                    final isUser = m.role == 'user';
                    return Align(
                      alignment:
                          isUser ? Alignment.centerRight : Alignment.centerLeft,
                      child: Container(
                        margin: const EdgeInsets.symmetric(vertical: 4),
                        padding: const EdgeInsets.symmetric(
                            horizontal: 14, vertical: 10),
                        constraints: BoxConstraints(
                            maxWidth: MediaQuery.of(context).size.width * 0.7),
                        decoration: BoxDecoration(
                          color: isUser
                              ? theme.colorScheme.primaryContainer
                              : theme.colorScheme.surfaceContainerHighest,
                          borderRadius: BorderRadius.circular(14),
                        ),
                        child: SelectableText(m.text),
                      ),
                    );
                  },
                ),
        ),
        SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 4, 12, 12),
            child: Row(children: [
              _MicButton(
                recording: _pttState == 'recording',
                enabled: _connected && !busy,
                onDown: () => _sendPtt('ptt_start'),
                onUp: () => _sendPtt('ptt_stop'),
                onCancel: () => _sendPtt('ptt_stop'),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: TextField(
                  controller: _input,
                  enabled: _connected && !_pttState.startsWith('r'),
                  decoration: InputDecoration(
                    hintText: _connected
                        ? (busy
                            ? (_speaking
                                ? 'Jarvis is speaking…'
                                : 'Jarvis is thinking…')
                            : 'Type a message or hold 🎤')
                        : 'Backend offline — start it and reconnect',
                    border: const OutlineInputBorder(),
                    isDense: true,
                  ),
                  onSubmitted: (_) => _send(),
                ),
              ),
              const SizedBox(width: 8),
              IconButton.filled(
                icon: const Icon(Icons.send),
                onPressed: (_connected && !busy) ? _send : null,
              ),
            ]),
          ),
        ),
      ]),
    );
  }
}

/// Circular hold-to-talk button. Holds fire ptt_start; release fires ptt_stop.
class _MicButton extends StatelessWidget {
  const _MicButton({
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
    final color = enabled
        ? (recording ? Colors.redAccent : theme.colorScheme.secondaryContainer)
        : theme.colorScheme.surfaceContainerHighest;
    return Material(
      color: color,
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTapDown: enabled ? (_) => onDown() : null,
        onTapUp: enabled ? (_) => onUp() : null,
        onTapCancel: enabled ? onCancel : null,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Icon(
            recording ? Icons.stop_circle : Icons.mic,
            color: enabled
                ? (recording ? Colors.white : theme.colorScheme.onSecondaryContainer)
                : theme.disabledColor,
          ),
        ),
      ),
    );
  }
}