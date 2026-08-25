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

    switch (decoded['type']) {
      case 'token':
        setState(() {
          if (!_assistantTyping) {
            _messages.add(ChatMessage(role: 'assistant', text: ''));
            _assistantTyping = true;
          }
          _messages.last.text += (decoded['text'] ?? '') as String;
        });
      case 'done':
        setState(() => _assistantTyping = false);
      case 'reset_done':
        setState(() => _messages.clear());
      default:
        break;
    }
  }

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty || !_connected) return;
    setState(() {
      _messages.add(ChatMessage(role: 'user', text: text));
      _assistantTyping = true;
      _input.clear();
    });
    _channel!.sink.add(jsonEncode({'type': 'user_message', 'text': text}));
  }

  void _reset() {
    if (_connected) {
      _channel!.sink.add(jsonEncode({'type': 'reset'}));
    } else {
      setState(_messages.clear);
    }
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
        Expanded(
          child: _messages.isEmpty
              ? Center(
                  child: Text('Phase 0: text chat via local LLM',
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
              Expanded(
                child: TextField(
                  controller: _input,
                  enabled: _connected,
                  decoration: InputDecoration(
                    hintText: _connected
                        ? (_assistantTyping ? 'Jarvis is thinking…' : 'Type a message')
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
                onPressed: _connected ? _send : null,
              ),
            ]),
          ),
        ),
      ]),
    );
  }
}
