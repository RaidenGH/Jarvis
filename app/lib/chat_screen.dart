import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

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

    // Change greeting to be more casual
    if (map['type'] == 'greeting') {
      ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(SnackBar(content: Text('Hey there!')));
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
      setState(() => _messages.clear);
    }
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

  String? get _statusLine {
    if (_pttState == 'recording') return 'Recording… release when done';
    if (_pttState == 'transcribing') return 'Transcribing…';
    if (_speaking) return 'Speaking…';
    if (_listening) return 'Listening for "$_wakeWord"…';
    return null;
  }

  void dispose() {
    _sub?.cancel();
    _channel?.sink.close(status.normalClosure);
    _input.dispose();
    super.dispose();
  }

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
          Text(_connected ? 'Connected' : 'Disconnected'),
        ]),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: _reset,
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: ListView.builder(
              reverse: true,
              itemCount: _messages.length,
              itemBuilder: (context, index) {
                final message = _messages[index];
                return ListTile(
                  title: Text(message.text),
                  subtitle: Text(message.role),
                );
              },
            ),
          ),
          Padding(
            padding: const EdgeInsets.all(8.0),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _input,
                    decoration: InputDecoration(
                      labelText: 'Type a message',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ),
                IconButton(
                  icon: const Icon(Icons.send),
                  onPressed: _send,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

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
        onTapDown: (details) => onDown(),
        onTapUp: (details) => onUp(),
        onTapCancel: () => onCancel(),
        child: Icon(
          Icons.mic,
          color: Colors.white,
          size: 24,
        ),
      ),
    );
  }
}
