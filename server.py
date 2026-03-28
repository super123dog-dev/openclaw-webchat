#!/usr/bin/env python3
"""
WebChat with Model Manager & Channel Management for OpenClaw
"""
import json
import os
import re
import shlex
import subprocess
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8080

def _terminal_qr_to_png(text):
    """Convert terminal QR code (Unicode block characters) to a PNG image (base64).

    Terminal QR codes use half-block characters where each character encodes
    two vertical pixels:
      '█' (full block)  = top BLACK, bottom BLACK
      '▀' (upper half)  = top BLACK, bottom WHITE
      '▄' (lower half)  = top WHITE, bottom BLACK
      ' ' (space)       = top WHITE, bottom WHITE

    Returns base64-encoded PNG string, or None on failure.
    """
    import base64, io, struct, zlib

    # Parse characters into a pixel grid
    lines = text.split('\n')
    # Filter to lines containing QR block characters
    qr_chars = {'█', '▀', '▄', '░', '▌', '▐', '▊', '▍'}
    qr_lines = []
    for line in lines:
        if any(c in line for c in qr_chars):
            qr_lines.append(line)
    if not qr_lines:
        return None

    # Each text line = 2 pixel rows (half-block encoding)
    width = max(len(line) for line in qr_lines)
    height = len(qr_lines) * 2
    # Build pixel grid (True = black)
    pixels = []
    for line in qr_lines:
        top_row = []
        bot_row = []
        for i in range(width):
            ch = line[i] if i < len(line) else ' '
            if ch == '█' or ch == '░':
                top_row.append(True)
                bot_row.append(True)
            elif ch == '▀':
                top_row.append(True)
                bot_row.append(False)
            elif ch == '▄':
                top_row.append(False)
                bot_row.append(True)
            else:
                top_row.append(False)
                bot_row.append(False)
        pixels.append(top_row)
        pixels.append(bot_row)

    # Scale up for scannability
    scale = 8
    sw = width * scale
    sh = height * scale

    # Build PNG manually (no PIL dependency)
    def _png_chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

    # IHDR
    ihdr = struct.pack('>IIBBBBB', sw, sh, 1, 0, 0, 0, 0)  # 1-bit grayscale
    # IDAT: build scanlines
    raw_rows = []
    for py in range(sh):
        src_y = py // scale
        row_bits = []
        for px in range(sw):
            src_x = px // scale
            row_bits.append(0 if pixels[src_y][src_x] else 1)  # 0=black, 1=white in grayscale
        # Pack bits into bytes (1-bit per pixel, MSB first)
        row_bytes = bytearray()
        for bi in range(0, len(row_bits), 8):
            byte = 0
            for bit_idx in range(8):
                if bi + bit_idx < len(row_bits):
                    byte |= (row_bits[bi + bit_idx] << (7 - bit_idx))
            row_bytes.append(byte)
        raw_rows.append(b'\x00' + bytes(row_bytes))  # filter byte 0 (None)

    idat_data = zlib.compress(b''.join(raw_rows))

    png = b'\x89PNG\r\n\x1a\n'
    png += _png_chunk(b'IHDR', ihdr)
    png += _png_chunk(b'IDAT', idat_data)
    png += _png_chunk(b'IEND', b'')

    return base64.b64encode(png).decode()

# Reliable openclaw subprocess runner (uses start_new_session to avoid PTY hangs)
def _run(cmd, timeout=30):
    """Run a command with timeout, using start_new_session to avoid subprocess hangs."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    except Exception as e:
        return -1, '', str(e)

# Find openclaw.json in possible locations
def find_openclaw_config():
    possible_paths = [
        os.path.expanduser("~/.openclaw/openclaw.json"),
        os.path.expanduser("~/.config/openclaw/openclaw.json"),
        "/etc/openclaw/openclaw.json",
    ]
    for p in possible_paths:
        if os.path.exists(p):
            return p
    return possible_paths[0]  # fallback

CLAW = find_openclaw_config()
CREDS_DIR = os.path.expanduser("~/.openclaw/credentials")
SESSIONS_FILE = os.path.join(DIR, "sessions.json")
USERS_FILE = os.path.join(DIR, "users.json")

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def verify_user(username, password):
    users = load_users()
    return users.get('users', {}).get(username) == password

# ========== Dynamic Provider Info ==========
_PROVIDER_INFO_CACHE = None

def get_provider_display_names():
    """Get provider display name from openclaw.json"""
    config = load_config()
    providers = config.get('models', {}).get('providers', {})
    
    # Known provider display name mapping
    provider_map = {
        'openrouter': 'OpenRouter',
        'google': 'Google',
        'anthropic': 'Anthropic', 
        'openai': 'OpenAI',
        'azure-openai': 'Azure OpenAI',
        'azure-openai-responses': 'Azure OpenAI (Responses)',
        'minimax-cn': 'MiniMax-CN',
        'deepseek': 'DeepSeek',
        'groq': 'Groq',
        'cerebras': 'Cerebras',
        'cohere': 'Cohere',
        'mistralai': 'Mistral AI',
        'amazon-bedrock': 'Amazon Bedrock',
        'novita': 'Novita AI',
        'nebius': 'Nebius',
        'togetherai': 'Together AI'
    }
    
    display_names = {}
    key_urls = {}
    
    for p in providers.keys():
        display_names[p] = provider_map.get(p, p.replace('-', ' ').title())
        key_urls[p] = ''
    
    return display_names

def get_provider_key_urls():
    """Get provider key URL from openclaw.json"""
    # Return empty key URLs (user needs to get API key themselves)
    return {}

# ========== Constants (will be loaded dynamically) ==========
# Provider info will be loaded from openclaw.json dynamically

# ========== Config Functions ==========
def load_config():
    if os.path.exists(CLAW):
        with open(CLAW, 'r') as f:
            content = f.read()
            return json.loads(content)
    return {}

# Sensitive keys to mask in config
sensitive_keys = {'botToken', 'token', 'password', 'appSecret', 'clientSecret',
                  'privateKey', 'appPassword', 'accessToken', 'signingSecret',
                  'channelSecret', 'channelAccessToken'}


def save_config(config):
    with open(CLAW, 'w') as f:
        json.dump(config, f, indent=2)

def load_sessions():
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_sessions(sessions):
    with open(SESSIONS_FILE, 'w') as f:
        json.dump(sessions, f, indent=2)

# ========== Model Functions ==========
# Find models.generated.js in possible locations
def find_models_file():
    possible_paths = [
        '/usr/lib/node_modules/openclaw/node_modules/@mariozechner/pi-ai/dist/models.generated.js',
        os.path.expanduser('~/.npm/_npx/*/node_modules/@mariozechner/pi-ai/dist/models.generated.js'),
        '/opt/openclaw/node_modules/@mariozechner/pi-ai/dist/models.generated.js',
    ]
    for p in possible_paths:
        import glob
        matched = glob.glob(p)
        if matched and os.path.exists(matched[0]):
            return matched[0]
    # Try to find via npm prefix
    try:
        result = subprocess.run(['npm', 'root', '-g'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            npm_root = result.stdout.strip()
            candidate = os.path.join(npm_root, '@mariozechner/pi-ai/dist/models.generated.js')
            if os.path.exists(candidate):
                return candidate
    except: pass
    return possible_paths[0]  # fallback to default

MODELS_FILE = find_models_file()

def parse_openclaw_models():
    """Parse OpenClaw's models.generated.js to get provider->models mapping"""
    models = {}
    try:
        with open(MODELS_FILE, 'r') as f:
            lines = f.readlines()
        
        current_provider = None
        
        for line in lines:
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            
            # Skip export line
            if 'export const' in stripped:
                continue
            
            # Provider keys at indent 4
            if indent == 4 and stripped.startswith('"') and stripped.endswith('": {'):
                import re
                match = re.match(r'^"([a-z0-9-]+)":\s*\{$', stripped)
                if match:
                    current_provider = match.group(1)
                    if current_provider not in models:
                        models[current_provider] = []
            
            # Model IDs at indent 12
            if current_provider and indent == 12:
                import re
                match = re.search(r'^id:\s*"([^"]+)"', stripped)
                if match:
                    model_id = match.group(1)
                    if model_id not in models[current_provider]:
                        models[current_provider].append(model_id)
                        
    except Exception as e:
        print(f"Error parsing models file: {e}")
    
    return models

# Cache the parsed models
_openclaw_models_cache = None

def get_openclaw_models():
    global _openclaw_models_cache
    if _openclaw_models_cache is None:
        _openclaw_models_cache = parse_openclaw_models()
    return _openclaw_models_cache

def get_openrouter_models():
    url = "https://openrouter.ai/api/v1/models"
    try:
        req = urllib.request.Request(url)
        r = urllib.request.urlopen(req, timeout=10)
        data = json.loads(r.read().decode())
        models = []
        for m in data.get('data', []):
            models.append({
                'id': m['id'],
                'name': m.get('name', m['id']),
                'provider': 'openrouter'
            })
        return models
    except Exception as e:
        print(f"Error fetching OpenRouter models: {e}")
        return []

def verify_api_key(provider, api_key):
    if not api_key:
        return False
    try:
        if provider == 'openrouter':
            url = "https://openrouter.ai/api/v1/models"
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {api_key}'})
            r = urllib.request.urlopen(req, timeout=5)
            return r.status == 200
        elif provider == 'google':
            url = f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
            req = urllib.request.Request(url)
            r = urllib.request.urlopen(req, timeout=5)
            return r.status == 200
        elif provider == 'anthropic':
            url = "https://api.anthropic.com/v1/messages"
            req = urllib.request.Request(url, headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01'
            }, method='HEAD')
            r = urllib.request.urlopen(req, timeout=5)
            return r.status == 200
        elif provider == 'openai':
            url = "https://api.openai.com/v1/models"
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {api_key}'})
            r = urllib.request.urlopen(req, timeout=5)
            return r.status == 200
    except:
        pass
    return True  # Assume valid if can.t verify

# ========== Channel Registry & Management ==========
#
# Comprehensive channel registry derived from OpenClaw documentation and
# `openclaw plugins list` output. Defines every supported channel with:
#   - How to install it (bundled enable vs npm install)
#   - What credentials/config it needs
#   - Whether it uses QR login or token-based auth

# DEPRECATED - Kept for backward compatibility only. Use get_all_channels() instead.
CHANNEL_REGISTRY = {
    # ── Chat Platforms ──────────────────────────────────────────────────────
    'telegram': {
        'name': 'Telegram', 'icon': '✈️', 'iconClass': 'telegram',
        'desc': 'Bot API token via BotFather',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'botToken', 'label': 'Bot Token', 'type': 'password', 'required': True,
             'placeholder': '123456:ABC-DEF...',
             'help': 'Get from @BotFather in Telegram'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/telegram',
    },
    'whatsapp': {
        'name': 'WhatsApp', 'icon': '💬', 'iconClass': 'whatsapp',
        'desc': 'Link via QR code (personal or dedicated number)',
        'requiresQR': True, 'installType': 'npm',
        'npmPackage': '@openclaw/whatsapp',
        'configFields': [
            {'name': 'dmPolicy', 'label': 'DM Policy', 'type': 'select',
             'options': ['pairing', 'allowlist', 'open'],
             'default': 'pairing', 'required': False},
            {'name': 'allowFrom', 'label': 'Allowed Numbers', 'type': 'text',
             'placeholder': '+15551234567 (E.164 format, comma-separated)',
             'required': False, 'help': 'E.164 numbers for allowlist mode'},
        ],
        'loginCmd': 'openclaw channels login --channel whatsapp',
        'docs': 'https://docs.openclaw.ai/channels/whatsapp',
    },
    'discord': {
        'name': 'Discord', 'icon': '🎮', 'iconClass': 'discord',
        'desc': 'Bot token from Discord Developer Portal',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'token', 'label': 'Bot Token', 'type': 'password',
             'required': True, 'placeholder': 'MTI...',
             'help': 'Bot token from Discord Developer Portal'},
            {'name': 'dmPolicy', 'label': 'DM Policy', 'type': 'select',
             'options': ['pairing', 'allowlist', 'open'],
             'default': 'pairing', 'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/discord',
    },
    'signal': {
        'name': 'Signal', 'icon': '📱', 'iconClass': 'signal',
        'desc': 'signal-cli account linked to the bot phone number',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'account', 'label': 'Signal Account (E.164)', 'type': 'text',
             'required': True, 'placeholder': '+15551234567',
             'help': 'Phone number registered with signal-cli'},
            {'name': 'cliPath', 'label': 'signal-cli Path', 'type': 'text',
             'default': 'signal-cli', 'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/signal',
    },
    'slack': {
        'name': 'Slack', 'icon': '💼', 'iconClass': 'slack',
        'desc': 'App Token (xapp-...) + Bot Token (xoxb-...) from Slack API',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'appToken', 'label': 'App Token (xapp-...)', 'type': 'password',
             'required': True, 'placeholder': 'xapp-...',
             'help': 'From Slack App Token with connections:write scope'},
            {'name': 'botToken', 'label': 'Bot Token (xoxb-...)', 'type': 'password',
             'required': True, 'placeholder': 'xoxb-...',
             'help': 'Bot User OAuth Token from Slack'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/slack',
    },
    'irc': {
        'name': 'IRC', 'icon': '💬', 'iconClass': 'irc',
        'desc': 'IRC server connection settings',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'server', 'label': 'Server', 'type': 'text',
             'required': True, 'placeholder': 'irc.libera.chat'},
            {'name': 'port', 'label': 'Port', 'type': 'number',
             'default': '6697', 'required': False},
            {'name': 'tls', 'label': 'Use TLS', 'type': 'checkbox',
             'default': True, 'required': False},
            {'name': 'nickname', 'label': 'Nickname', 'type': 'text',
             'required': True, 'placeholder': 'MyBot'},
            {'name': 'username', 'label': 'Username', 'type': 'text',
             'required': False},
            {'name': 'password', 'label': 'Server Password', 'type': 'password',
             'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/irc',
    },
    'line': {
        'name': 'LINE', 'icon': '💬', 'iconClass': 'line',
        'desc': 'Long-lived channel access token from LINE Developers',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'channelToken', 'label': 'Channel Access Token', 'type': 'password',
             'required': True, 'placeholder': 'long-lived token from LINE Developers'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/line',
    },
    'matrix': {
        'name': 'Matrix', 'icon': '💬', 'iconClass': 'matrix',
        'desc': 'Homeserver URL + access token from any Matrix client',
        'requiresQR': False, 'installType': 'npm',
        'npmPackage': '@openclaw/matrix',
        'configFields': [
            {'name': 'homeserver', 'label': 'Homeserver URL', 'type': 'text',
             'required': True, 'placeholder': 'https://matrix.org'},
            {'name': 'accessToken', 'label': 'Access Token', 'type': 'password',
             'required': True, 'placeholder': 'MDA... (from any Matrix client)'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/matrix',
    },
    'mattermost': {
        'name': 'Mattermost', 'icon': '💬', 'iconClass': 'mattermost',
        'desc': 'Personal Access Token from Mattermost user settings',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'endpoint', 'label': 'Server URL', 'type': 'text',
             'required': True, 'placeholder': 'https://mattermost.example.com'},
            {'name': 'token', 'label': 'Personal Access Token', 'type': 'password',
             'required': True},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/mattermost',
    },
    'msteams': {
        'name': 'MS Teams', 'icon': '👥', 'iconClass': 'msteams',
        'desc': 'App ID + Client Secret from Azure AD App Registration',
        'requiresQR': False, 'installType': 'npm',
        'npmPackage': '@openclaw/msteams',
        'configFields': [
            {'name': 'appId', 'label': 'Application (client) ID', 'type': 'text',
             'required': True,
             'placeholder': '12345678-1234-1234-1234... (Azure AD)'},
            {'name': 'tenantId', 'label': 'Tenant ID', 'type': 'text',
             'required': True, 'placeholder': '12345678-1234-1234-1234...'},
            {'name': 'appPassword', 'label': 'Client Secret', 'type': 'password',
             'required': True},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/msteams',
    },
    'googlechat': {
        'name': 'Google Chat', 'icon': '💬', 'iconClass': 'googlechat',
        'desc': 'Service account JSON key file path',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'serviceAccountJson', 'label': 'Service Account JSON Path',
             'type': 'text', 'required': True,
             'placeholder': '/path/to/service-account.json'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/googlechat',
    },
    'feishu': {
        'name': 'Feishu / Lark', 'icon': '📱', 'iconClass': 'feishu',
        'desc': 'App ID + App Secret from Feishu Open Platform',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'appId', 'label': 'App ID', 'type': 'text',
             'required': True, 'placeholder': 'cli_...'},
            {'name': 'appSecret', 'label': 'App Secret', 'type': 'password',
             'required': True},
            {'name': 'verificationToken', 'label': 'Verification Token',
             'type': 'password', 'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/feishu',
    },
    'nostr': {
        'name': 'Nostr', 'icon': '⚡', 'iconClass': 'nostr',
        'desc': 'NIP-04 encrypted DMs — public key required, nsec optional',
        'requiresQR': False, 'installType': 'npm',
        'npmPackage': '@openclaw/nostr',
        'configFields': [
            {'name': 'publicKey', 'label': 'Public Key (npub...)', 'type': 'text',
             'required': True, 'placeholder': 'npub1...'},
            {'name': 'privateKey', 'label': 'Private Key (nsec...) [optional]',
             'type': 'password', 'required': False,
             'placeholder': 'nsec1... (enables posting)'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/nostr',
    },
    'zalo': {
        'name': 'Zalo', 'icon': '💬', 'iconClass': 'zalo',
        'desc': 'Zalo Official Account API credentials',
        'requiresQR': False, 'installType': 'npm',
        'npmPackage': '@openclaw/zalo',
        'configFields': [
            {'name': 'appId', 'label': 'App ID', 'type': 'text', 'required': True},
            {'name': 'appSecret', 'label': 'App Secret', 'type': 'password',
             'required': True},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/zalo',
    },
    'zalouser': {
        'name': 'Zalo Personal', 'icon': '💬', 'iconClass': 'zalouser',
        'desc': 'Zalo Personal Account via QR code login',
        'requiresQR': True, 'installType': 'npm',
        'npmPackage': '@openclaw/zalouser',
        'configFields': [],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/zalouser',
    },
    # ── Apple / macOS ───────────────────────────────────────────────────────
    'bluebubbles': {
        'name': 'BlueBubbles', 'icon': '🍎', 'iconClass': 'bluebubbles',
        'desc': 'iMessage via BlueBubbles server (macOS)',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'serverUrl', 'label': 'BlueBubbles Server URL', 'type': 'text',
             'required': True, 'placeholder': 'wss://your-mac.local:1234'},
            {'name': 'password', 'label': 'Server Password', 'type': 'password',
             'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/bluebubbles',
    },
    'imessage': {
        'name': 'iMessage', 'icon': '🍎', 'iconClass': 'imessage',
        'desc': 'iMessage via BlueBubbles (same setup, different UI)',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'serverUrl', 'label': 'BlueBubbles Server URL', 'type': 'text',
             'required': True},
            {'name': 'password', 'label': 'Server Password', 'type': 'password',
             'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/imessage',
    },
    # ── Enterprise / Self-hosted ────────────────────────────────────────────
    'nextcloud-talk': {
        'name': 'Nextcloud Talk', 'icon': '☁️', 'iconClass': 'nextcloud',
        'desc': 'Nextcloud instance URL + app password',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'serverUrl', 'label': 'Nextcloud URL', 'type': 'text',
             'required': True, 'placeholder': 'https://nextcloud.example.com'},
            {'name': 'username', 'label': 'Username', 'type': 'text',
             'required': True},
            {'name': 'password', 'label': 'App Password', 'type': 'password',
             'required': True},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/nextcloud',
    },
    'synology-chat': {
        'name': 'Synology Chat', 'icon': '💬', 'iconClass': 'synology',
        'desc': 'Synology Chat Server webhook URL',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'webhookUrl', 'label': 'Webhook URL', 'type': 'text',
             'required': True,
             'placeholder': 'https://your-nas:5000/webapi/...'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/synology',
    },
    'tlon': {
        'name': 'Tlon / Urbit', 'icon': '🌙', 'iconClass': 'tlon',
        'desc': 'Urbit ship name + URL + code',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'ship', 'label': 'Urbit Ship (e.g. ~zod)', 'type': 'text',
             'required': True},
            {'name': 'url', 'label': 'Lroor URL', 'type': 'text', 'required': True},
            {'name': 'code', 'label': 'Access Code', 'type': 'password',
             'required': False},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/tlon',
    },
    'twitch': {
        'name': 'Twitch', 'icon': '🎮', 'iconClass': 'twitch',
        'desc': 'Twitch Bot OAuth token (IRC chat)',
        'requiresQR': False, 'installType': 'bundled',
        'npmPackage': None,
        'configFields': [
            {'name': 'nickname', 'label': 'Bot Username', 'type': 'text',
             'required': True},
            {'name': 'token', 'label': 'OAuth Token', 'type': 'password',
             'required': True, 'placeholder': 'oauth:...'},
        ],
        'loginCmd': None,
        'docs': 'https://docs.openclaw.ai/channels/twitch',
    },
}

# ── Plugin status cache ────────────────────────────────────────────────────────

_plugin_status_cache = {}   # {plugin_id: 'loaded'|'disabled'|'not_found'}
_plugin_status_time = 0
_PLUGIN_CACHE_TTL = 300     # 5 minutes

def _parse_plugin_list():
    """Parse `openclaw plugins list` text output to get plugin ID → status map.
    Returns dict: {plugin_id: {'status': 'loaded'|'disabled', 'origin': 'bundled'|'config'|'stock'}}}
    """
    import re
    try:
        rc, stdout, stderr = _run(['openclaw', 'plugins', 'list'], timeout=30)
        output = stdout + stderr

        # Map truncated IDs to canonical plugin IDs (from Name column analysis)
        # Key: truncated ID col value → canonical plugin id
        ID_MAPPING = {
            # Truncated ID column → canonical plugin ID
            'bluebubb': 'bluebubbles',
            'mattermo': 'mattermost',
            'nextclou': 'nextcloud-talk',
            'googlech': 'googlechat',
            'copilot-': 'copilot-proxy',
            'diagnost': 'diagnostics-otel',
            'minimax-': 'minimax-portal-auth',
            'thread-o': 'thread-ownership',
            'synology': 'synology-chat',
            # These full IDs also appear in the ID column directly
            'telegram': 'telegram',
            'discord': 'discord',
            'signal': 'signal',
            'slack': 'slack',
            'irc': 'irc',
            'line': 'line',
            'matrix': 'matrix',
            'mattermost': 'mattermost',
            'msteams': 'msteams',
            'feishu': 'feishu',
            'nostr': 'nostr',
            'zalo': 'zalo',
            'zalouser': 'zalouser',
            'imessage': 'imessage',
            'tlon': 'tlon',
            'twitch': 'twitch',
            'whatsapp': 'whatsapp',
            'qqbot': 'qqbot',
        }

        status_map = {}
        for line in output.split('\n'):
            if '│' not in line or 'Name' in line or '─' in line:
                continue
            parts = [p.strip() for p in line.split('│')]
            if len(parts) < 4:
                continue
            name_col = parts[1].strip()   # may be like "@openclaw/discord"
            id_col   = parts[2].strip()   # truncated to ~10 chars
            status   = parts[3].strip()   # "loaded" | "disabled" | ""
            source   = parts[4].strip() if len(parts) > 4 else ''

            if not id_col or id_col == 'ID':
                # Try to derive plugin id from name
                if name_col.startswith('@openclaw/'):
                    pid = name_col.split('@openclaw/')[1].strip()
                elif name_col in ID_MAPPING.values():
                    pid = name_col.lower().replace(' ', '').replace('-', '')
                    # Map known ids
                    for kw, cid in [('telegram','telegram'),('whatsapp','whatsapp'),
                                     ('discord','discord'),('signal','signal'),
                                     ('slack','slack'),('irc','irc'),('line','line'),
                                     ('matrix','matrix'),('mattermost','mattermost'),
                                     ('msteams','msteams'),('googlechat','googlechat'),
                                     ('feishu','feishu'),('nostr','nostr'),('zalo','zalo'),
                                     ('imessage','imessage'),('nextcloud','nextcloud-talk'),
                                     ('synology','synology-chat'),('tlon','tlon'),
                                     ('twitch','twitch')]:
                        if kw in name_col.lower():
                            pid = cid
                            break
                else:
                    continue
            else:
                pid = ID_MAPPING.get(id_col, id_col)

            if pid:
                status_map[pid] = {
                    'status': status if status in ('loaded', 'disabled') else 'disabled',
                    'origin': 'bundled' if 'stock:' in source else 'config',
                }
        return status_map
    except Exception as e:
        print(f"Error parsing plugin list: {e}")
        return {}

def _get_plugin_status():
    """Get cached plugin status, refreshing if stale.
    Falls back to config file parsing if subprocess hangs (avoids blocking)."""
    global _plugin_status_cache, _plugin_status_time
    import time
    if not _plugin_status_cache or (time.time() - _plugin_status_time) > _PLUGIN_CACHE_TTL:
        # Try fast subprocess call first (cached 5 min), fall back to config file
        try:
            _plugin_status_cache = _parse_plugin_list()
            _plugin_status_time = time.time()
        except Exception:
            # Subprocess blocked — derive from config file directly
            _plugin_status_cache = _get_plugin_status_from_config()
            _plugin_status_time = time.time()
    return _plugin_status_cache

def _get_plugin_status_from_config():
    """Derive plugin status directly from openclaw.json (no subprocess)."""
    cfg = load_config()
    plugins_cfg = cfg.get('plugins', {})
    entries = plugins_cfg.get('entries', {})
    # Load paths tell us which are bundled
    load_paths = plugins_cfg.get('load', {}).get('paths', [])
    bundled_paths = [p.rstrip('/').rsplit('/', 1)[-1] for p in load_paths]
    status = {}
    for ch_id in entries:
        status[ch_id] = {
            'status': 'loaded' if entries[ch_id].get('enabled', False) else 'disabled',
            'origin': 'bundled'
        }
    return status

def _get_channel_config_from_openclaw():
    """Get currently configured channels from openclaw.json directly (no subprocess)."""
    cfg = load_config()
    channels_cfg = cfg.get('channels', {})
    return {ch_id: {'configured': True} for ch_id, v in channels_cfg.items() if v.get('enabled', False)}

# ── Core channel API ──────────────────────────────────────────────────────────

def get_channel_config(channel_id):
    """Get current config for a specific channel from openclaw.json (masking secrets)."""
    cfg = load_config()
    ch_cfg = cfg.get('channels', {}).get(channel_id, {})
    # Mask sensitive fields
    masked = {}
    sensitive = {'botToken', 'token', 'password', 'appSecret', 'clientSecret', 'privateKey', 'appPassword', 'accessToken'}
    for k, v in ch_cfg.items():
        if k in sensitive and v:
            masked[k] = '••••••' + str(v)[-4:] if len(str(v)) > 4 else '••••'
        else:
            masked[k] = v
    return masked

def logout_channel(channel_id):
    """Disable a channel and remove its credentials."""
    cfg = load_config()
    if channel_id in cfg.get('channels', {}):
        cfg['channels'][channel_id]['enabled'] = False
        save_config(cfg)
    # Also remove cached credentials if any
    cred_path = os.path.join(CREDS_DIR, channel_id)
    if os.path.isdir(cred_path):
        import shutil
        shutil.rmtree(cred_path, ignore_errors=True)
    elif os.path.isfile(cred_path + '.json'):
        os.remove(cred_path + '.json')
def remove_channel(ch_id):
    """Remove a channel: disable plugin + remove config."""
    try:
        import subprocess
        # Disable plugin
        subprocess.run(['openclaw', 'plugins', 'disable', ch_id], capture_output=True, timeout=30)
        # Remove channel config
        subprocess.run(['openclaw', 'channels', 'remove', '--channel', ch_id, '--delete'], capture_output=True, timeout=30)
        # Also remove from openclaw.json
        cfg = load_config()
        if ch_id in cfg.get('channels', {}):
            del cfg['channels'][ch_id]
            save_config(cfg)
        # Restart gateway
        subprocess.Popen(['openclaw', 'gateway', 'restart'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f'Channel {ch_id} removed'
    except Exception as e:
        return False, str(e)


def install_channel(ch_id):
    """Install/enable a channel plugin."""
    try:
        import subprocess
        import json
        # Find plugin that provides this channel from plugins list
        result = subprocess.run(['openclaw', 'plugins', 'list', '--json'], capture_output=True, text=True, timeout=30)
        start = result.stdout.find('{')
        data = json.loads(result.stdout[start:])
        target_plugin = None
        for plugin in data.get('plugins', []):
            if ch_id in plugin.get('channelIds', []):
                target_plugin = plugin['id']
                break
        
        # If not found by channelIds, try using channel id as plugin name
        if not target_plugin:
            for plugin in data.get('plugins', []):
                if plugin.get('id') == ch_id or ch_id in plugin.get('channelIds', []):
                    target_plugin = plugin['id']
                    break
        
        # If still not found, use the channel id directly as package name
        if not target_plugin:
            target_plugin = ch_id
        
        # Check if it's an npm plugin (needs to be installed)
        plugin = next((p for p in data.get('plugins', []) if p.get('id') == target_plugin), None)
        npm_pkg = plugin.get('npmPackage') if plugin else None
        
        if npm_pkg:
            # Install via openclaw plugins install - e.g., "openclaw plugins install line"
            # Run in background to avoid blocking
            pkg_name = npm_pkg.split('/')[-1] if '/' in npm_pkg else npm_pkg.replace('@openclaw/', '')
            subprocess.Popen(['openclaw', 'plugins', 'install', pkg_name], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif target_plugin != ch_id:
            # Plugin exists but no npm package, just enable it
            subprocess.Popen(['openclaw', 'plugins', 'enable', target_plugin],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # Try installing channel id directly as package
            subprocess.Popen(['openclaw', 'plugins', 'install', ch_id],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Enable plugin - run in background
        subprocess.Popen(['openclaw', 'plugins', 'enable', target_plugin],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Restart gateway - run in background
        subprocess.Popen(['openclaw', 'gateway', 'restart'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f'Channel {ch_id} installation started via plugin {target_plugin}'
    except Exception as e:
        return False, str(e)


def configure_channel(ch_id, config_updates):
    """Configure a channel in openclaw.json."""
    try:
        cfg = load_config()
        if 'channels' not in cfg:
            cfg['channels'] = {}
        if ch_id not in cfg['channels']:
            cfg['channels'][ch_id] = {}
        cfg['channels'][ch_id].update(config_updates)
        save_config(cfg)
        # Restart gateway
        subprocess.Popen(['openclaw', 'gateway', 'restart'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f'Channel {ch_id} configured'
    except Exception as e:
        return False, str(e)


def get_all_channels():
    """Return ALL channel plugins from CHANNEL_REGISTRY + runtime status overlay."""
    
    try:
        import subprocess
        result = subprocess.run(['openclaw', 'plugins', 'list', '--json'], 
                              capture_output=True, text=True, timeout=30)
        output = result.stdout.strip()
        # Extract JSON part (from first '{' to last '}')
        start = output.find('{')
        end = output.rfind('}') + 1
        if start >= 0 and end > start:
            plugins_data = json.loads(output[start:end])
        else:
            raise ValueError("No JSON found")
    except Exception as e:
        print(f"Error getting plugins: {e}")
        plugins_data = {'plugins': []}
    
    openclaw_cfg = load_config()
    channels_cfg = openclaw_cfg.get('channels', {})
    
    # Build plugin status lookup (by plugin id AND by channel id)
    plugin_status_map = {}
    channel_to_plugin = {}  # channel id -> plugin id mapping
    for plugin in plugins_data.get('plugins', []):
        pid = plugin.get('id', '')
        plugin_status_map[pid] = plugin.get('status', 'not_installed')
        # Map each channel id to this plugin
        for ch_id in plugin.get('channelIds', []):
            channel_to_plugin[ch_id] = pid
    
    # Sensitive keys to mask
    sensitive_keys = {'botToken', 'token', 'password', 'appSecret', 'clientSecret',
                      'privateKey', 'appPassword', 'accessToken', 'signingSecret',
                      'channelSecret', 'channelAccessToken'}
    
    result = []
    
    # Iterate CHANNEL_REGISTRY to include all channels
    for ch_id, reg in CHANNEL_REGISTRY.items():
        # Get plugin id for this channel, then get its status
        plugin_id = channel_to_plugin.get(ch_id, '')
        plugin_status = plugin_status_map.get(plugin_id, 'not_installed')
        ch_cfg = channels_cfg.get(ch_id, {})
        enabled = ch_cfg.get('enabled', False)
        
        if plugin_status == 'loaded':
            if enabled:
                state = 'ready'
            else:
                state = 'installed_not_configured'
        elif plugin_status == 'disabled':
            state = 'installed_not_configured'
        else:
            state = 'not_installed'
        
        icon = reg.get('icon', '💬')
        requires_qr = reg.get('requiresQR', False)
        
        result.append({
            'id': ch_id,
            'name': reg.get('name', ch_id.title()),
            'icon': icon,
            'iconClass': reg.get('iconClass', ch_id),
            'desc': reg.get('desc', ''),
            'requiresQR': requires_qr,
            'authType': reg.get('authType', 'token'),
            'configFields': reg.get('configFields', []),
            'state': state,
            'config': {k: v for k, v in ch_cfg.items() if k not in sensitive_keys},
            'hasCredentials': bool(ch_cfg),
        })
    
    # Also add channels from plugins that are not in CHANNEL_REGISTRY
    existing_ch_ids = set(CHANNEL_REGISTRY.keys())
    for plugin in plugins_data.get('plugins', []):
        plugin_status = plugin.get('status', 'not_installed')
        for ch_id in plugin.get('channelIds', []):
            if ch_id in existing_ch_ids:
                continue
            if plugin_status != 'loaded':
                continue
            ch_cfg = channels_cfg.get(ch_id, {})
            enabled = ch_cfg.get('enabled', False)
            state = 'ready' if enabled else 'installed_not_configured'
            result.append({
                'id': ch_id,
                'name': plugin.get('name', ch_id.title()),
                'icon': '💬',
                'iconClass': ch_id,
                'desc': plugin.get('description', ''),
                'requiresQR': False,
                'authType': 'token',
                'configFields': [],
                'state': state,
                'config': {k: v for k, v in ch_cfg.items() if k not in sensitive_keys},
                'hasCredentials': bool(ch_cfg),
            })
            existing_ch_ids.add(ch_id)
    
    print(f"[channels] Returning {len(result)} channels ({sum(1 for c in result if c['state'] != 'not_installed')} active)")
    return result

class H(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def get_token(self):
        try:
            if os.path.exists(CLAW):
                c = json.load(open(CLAW))
                token = c.get('gateway',{}).get('auth',{}).get('token','')
                if token:
                    return token
        except: pass
        try:
            gw_port = 18789
            if os.path.exists(CLAW):
                c = json.load(open(CLAW))
                gw_port = c.get('gateway',{}).get('port',18789)
            req = urllib.request.Request(f"http://localhost:{gw_port}/auth/token")
            r = urllib.request.urlopen(req, timeout=5)
            data = json.loads(r.read().decode())
            return data.get('token','')
        except: pass
        return ''

    def get_models_list(self):
        models = {}
        try:
            if os.path.exists(CLAW):
                c = json.load(open(CLAW))
                for m in c.get('agents',{}).get('defaults',{}).get('models',{}):
                    models[m] = {}
                fallback = c.get('agents',{}).get('defaults',{}).get('model',{}).get('fallbacks',[])
                for fm in fallback:
                    if fm not in models:
                        models[fm] = {}
        except: pass
        if not models:
            models = {}
        return list(models.keys())

    def get_primary_model(self):
        try:
            if os.path.exists(CLAW):
                c = json.load(open(CLAW))
                primary = c.get('agents',{}).get('defaults',{}).get('model',{}).get('primary', '')
                if primary:
                    return primary
        except: pass
        # Fallback to M2.7 (the actual configured default)
        return ''

    def do_GET(self):
        # === Channel Status API (legacy - use /api/channels/all instead) ===
        if self.path == '/api/channels/status':
            # Build legacy format from CHANNEL_REGISTRY for backward compatibility
            channels = get_all_channels()
            result = {}
            for ch in channels:
                result[ch['id']] = {
                    'name': ch['name'],
                    'icon': ch['icon'],
                    'iconClass': ch['iconClass'],
                    'desc': ch['desc'],
                    'requiresQR': ch['requiresQR'],
                    'enabled': ch['state'] == 'ready',
                    'configured': ch['state'] == 'ready',
                    'running': ch['state'] == 'ready',
                    'linked': ch['hasCredentials'],
                    'requiresPlugin': ch['state'] == 'not_installed',
                    'loaded': ch['pluginStatus'] == 'loaded',
                }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # === All Channels (new comprehensive endpoint) ===
        if self.path == '/api/channels/all':
            channels = get_all_channels()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'channels': channels}).encode())
            return

        # === Channel Individual Status API ===
        if self.path.startswith('/api/channels/status/'):
            channel = self.path.split('/')[-1]
            all_ch = get_all_channels()
            result = {}
            for ch in all_ch:
                if ch['id'] == channel:
                    result = ch
                    break
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # === Channel Config API ===
        if self.path.startswith('/api/channels/config/'):
            channel = self.path.split('/')[-1]
            config = get_channel_config(channel)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(config).encode())
            return

        # === Model Manager API ===
        if self.path == '/api/models/providers':
            providers = get_openclaw_models()
            result = []
            for p, models in providers.items():
                if models:
                    result.append({
                        "id": p,
                        "name": get_provider_display_names().get(p, p),
                        "modelCount": len(models),
                        "keyUrl": get_provider_key_urls().get(p, "")
                    })
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
            return

        # === List Models for Provider ===
        if self.path.startswith('/api/models/list/'):
            provider = self.path.split('/')[-1]
            models_list = []
            try:
                # Get models from OpenClaw.s models.generated.js
                all_models = get_openclaw_models()
                models_list = all_models.get(provider, [])
            except Exception as e:
                print(f"Error getting models for {provider}: {e}")
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"models": models_list}).encode())
            return

        if self.path == '/api/models/primary':
            primary = self.get_primary_model()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"primary": primary}).encode())
            return

        if self.path == '/api/models/active':
            # Get models in the format frontend expects
            models_list = []
            try:
                if os.path.exists(CLAW):
                    c = json.load(open(CLAW))
                    primary = c.get('agents',{}).get('defaults',{}).get('model',{}).get('primary', '')
                    fallbacks = c.get('agents',{}).get('defaults',{}).get('model',{}).get('fallbacks', [])
                    
                    # Add models from defaults.models
                    for m in c.get('agents',{}).get('defaults',{}).get('models',{}):
                        if '/' in m:
                            provider, model_id = m.split('/', 1)
                            models_list.append({
                                'provider': provider,
                                'modelId': model_id,
                                'isPrimary': m == primary,
                                'isFallback': m in fallbacks,
                                'fallbackIndex': fallbacks.index(m) if m in fallbacks else None
                            })
                    
                    # Add fallback models not in defaults.models
                    for i, fm in enumerate(fallbacks):
                        if fm not in [m['provider']+'/'+m['modelId'] for m in models_list]:
                            if '/' in fm:
                                provider, model_id = fm.split('/', 1)
                                models_list.append({
                                    'provider': provider,
                                    'modelId': model_id,
                                    'isPrimary': fm == primary,
                                    'isFallback': True,
                                    'fallbackIndex': i
                                })
            except: pass
            
            # Don't auto-add fallback - return empty list if no models
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(models_list).encode())
            return

        # === Config API ===
        if self.path == '/config':
            print("[DEBUG] /config API called")
            cfg = {'models': {}, 'auth': {'token': '', 'gateway_url': ''}, 'primary': '', 'debug': {'claw_path': CLAW, 'exists': os.path.exists(CLAW)}}
            try:
                c = load_config()
                if c:
                    gw_port = c.get('gateway',{}).get('port',18789)
                    cfg['auth']['token'] = c.get('gateway',{}).get('auth',{}).get('token','')
                    cfg['auth']['gateway_url'] = f"http://localhost:{gw_port}"
                    
                    # Get primary model
                    primary_model = c.get('agents',{}).get('defaults',{}).get('model',{}).get('primary', '')
                    cfg['primary'] = primary_model
                    
                    for m in c.get('agents',{}).get('defaults',{}).get('models',{}):
                        cfg['models'][m] = {}
                    fallback_models = c.get('agents',{}).get('defaults',{}).get('model',{}).get('fallbacks',[])
                    for fm in fallback_models:
                        if fm not in cfg['models']:
                            cfg['models'][fm] = {}
            except: pass
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(cfg).encode())
            return

        # === Sessions API ===
        if self.path == '/sessions':
            sessions = load_sessions()
            session_list = {}
            for sid, entry in sessions.items():
                # Support both old format (list) and new format (dict with messages+updated)
                if isinstance(entry, list):
                    msgs = entry
                    updated = 0
                else:
                    msgs = entry.get('messages', [])
                    updated = entry.get('updated', 0)
                # Fallback: extract timestamp from session id
                if not updated and sid.startswith('session_'):
                    try:
                        updated = int(sid.split('_')[1]) / 1000
                    except (ValueError, IndexError):
                        pass
                title = ''
                for m in msgs:
                    if m.get('role') == 'user':
                        title = m.get('content', '')[:30]
                        break
                session_list[sid] = {'id': sid, 'title': title or 'New Chat', 'messages': msgs, 'updated': updated}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(session_list).encode())
            return

        # === Favicon ===
        if self.path == '/favicon.ico':
            self.send_response(200)
            self.send_header('Content-Type', 'image/x-icon')
            self.end_headers()
            self.wfile.write(b'')
            return

        # Default: serve static files
        SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        # === Execute Command API (for channel setup) ===
        if self.path == '/api/exec':
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl).decode() if cl > 0 else '{}'
            data = json.loads(body) if body else {}
            cmd = data.get('command', '')
            
            if not cmd:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No command provided'}).encode())
                return
            
            # Security: parse command safely and only allow openclaw commands
            try:
                cmd_parts = shlex.split(cmd)
            except ValueError:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Invalid command syntax'}).encode())
                return

            if not cmd_parts or cmd_parts[0] != 'openclaw':
                self.send_response(403)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Only openclaw commands allowed'}).encode())
                return

            try:
                proc = subprocess.Popen(cmd_parts, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                stdout, stderr = proc.communicate(timeout=60)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'success': proc.returncode == 0,
                    'stdout': stdout[:5000],
                    'stderr': stderr[:5000],
                    'returncode': proc.returncode
                }).encode())
            except subprocess.TimeoutExpired:
                self.send_response(408)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'Command timed out'}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        # === Channel Logout API ===
        if self.path == '/api/channels/logout':
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl).decode() if cl > 0 else '{}'
            data = json.loads(body) if body else {}
            channel = data.get('channel', '')
            if channel:
                logout_channel(channel)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True}).encode())
            else:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No channel specified'}).encode())
            return

        # === Channel Install API ===
        if self.path == '/api/channels/install':
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl).decode() if cl > 0 else '{}'
            data = json.loads(body) if body else {}
            channel_id = data.get('channel', '')
            if not channel_id:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No channel specified'}).encode())
                return
            success, message = install_channel(channel_id)
            self.send_response(200 if success else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': success, 'message': message}).encode())
            return

        # === Channel Configure API (generic) ===
        if self.path == '/api/channels/configure':
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl).decode() if cl > 0 else '{}'
            data = json.loads(body) if body else {}
            channel_id = data.get('channel', '')
            config_updates = data.get('config', {})
            if not channel_id:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No channel specified'}).encode())
                return
            success, message = configure_channel(channel_id, config_updates)
            self.send_response(200 if success else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': success, 'message': message}).encode())
            return

        # === Channel Remove API ===
        if self.path == '/api/channels/remove':
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl).decode() if cl > 0 else '{}'
            data = json.loads(body) if body else {}
            channel_id = data.get('channel', '')
            if not channel_id:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No channel specified'}).encode())
                return
            success, message = remove_channel(channel_id)
            self.send_response(200 if success else 500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'success': success, 'message': message}).encode())
            return

        # === Channel Login API (QR code channels: WhatsApp, Zalo Personal) ===
        if self.path == '/api/channels/login':
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl).decode() if cl > 0 else '{}'
            data = json.loads(body) if body else {}
            channel = data.get('channel', '')
            account = data.get('account', '')

            # Get channel info dynamically
            channels = get_all_channels()
            ch_info = next((c for c in channels if c['id'] == channel), None)
            if not ch_info or not ch_info.get('requiresQR'):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": f"Channel '{channel}' does not use QR login"}).encode())
                return

            import base64

            # 1) Check for an existing recent QR image file (zalouser writes PNG)
            qr_image_patterns = [
                f'/tmp/openclaw/openclaw-{channel}-qr-{account or "default"}.png',
                f'/tmp/openclaw/openclaw-{channel}-qr.png',
                os.path.expanduser(f'~/.openclaw/credentials/{channel}/qr.png'),
            ]
            for qr_path in qr_image_patterns:
                if os.path.exists(qr_path) and (time.time() - os.path.getmtime(qr_path)) < 300:
                    with open(qr_path, 'rb') as f:
                        qr_data = base64.b64encode(f.read()).decode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "status": "qr",
                        "qrData": f"data:image/png;base64,{qr_data}",
                        "message": f"Scan this QR code with your {reg['name']} app"
                    }).encode())
                    return

            # 2) Launch login process in background (it blocks waiting for QR scan)
            #    Use a reader thread to collect stdout without blocking
            cmd = ['openclaw', 'channels', 'login', '--channel', channel]
            if account:
                cmd += ['--account', account]

            try:
                import threading

                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # merge stderr into stdout
                    text=True,
                    start_new_session=True
                )

                # Reader thread: continuously read stdout into a shared buffer
                output_buf = []
                read_done = threading.Event()

                def _reader():
                    try:
                        for line in proc.stdout:
                            output_buf.append(line)
                    except Exception:
                        pass
                    finally:
                        read_done.set()

                t = threading.Thread(target=_reader, daemon=True)
                t.start()

                # Poll up to 15 seconds for QR (file or stdout)
                deadline = time.time() + 15
                responded = False

                while time.time() < deadline:
                    # Check if a QR image file was created
                    for qr_path in qr_image_patterns:
                        if os.path.exists(qr_path) and (time.time() - os.path.getmtime(qr_path)) < 60:
                            with open(qr_path, 'rb') as f:
                                qr_data = base64.b64encode(f.read()).decode()
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({
                                "status": "qr",
                                "qrData": f"data:image/png;base64,{qr_data}",
                                "message": f"Scan this QR code with your {reg['name']} app"
                            }).encode())
                            responded = True
                            break
                    if responded:
                        break

                    # Check stdout buffer for QR block characters
                    current_output = ''.join(output_buf)
                    qr_chars = {'█', '▀', '▄', '░', '▌', '▐', '▊', '▍'}
                    if current_output and any(c in current_output for c in qr_chars):
                        # QR started appearing — wait a bit more for it to finish
                        time.sleep(2)
                        current_output = ''.join(output_buf)
                        # Convert terminal QR text to PNG image
                        qr_image_data = _terminal_qr_to_png(current_output)
                        if qr_image_data:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({
                                "status": "qr",
                                "qrData": f"data:image/png;base64,{qr_image_data}",
                                "message": f"Scan this QR code with your {reg['name']} app"
                            }).encode())
                        else:
                            # Fallback: return raw text
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            self.wfile.write(json.dumps({
                                "status": "terminal_qr",
                                "qrText": current_output,
                                "message": f"Scan this QR code with your {reg['name']} app"
                            }).encode())
                        responded = True
                        break

                    # Check if process exited early (error)
                    if read_done.is_set() or proc.poll() is not None:
                        break

                    time.sleep(0.5)

                if not responded:
                    output = ''.join(output_buf)
                    login_cmd = f"openclaw channels login --channel {channel}"
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "status": "manual",
                        "command": login_cmd,
                        "output": output[:3000] if output else '',
                        "message": f"Could not capture QR automatically. Run in terminal: {login_cmd}"
                    }).encode())
                # Don't kill the login process — it stays alive waiting for scan
            except Exception as e:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode())
            return

        # === Model Manager API ===
        if self.path == '/api/models/verify':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            provider = data.get('provider', '')
            api_key = data.get('apiKey', '')
            is_valid = verify_api_key(provider, api_key)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"valid": is_valid}).encode())
            return

        if self.path == '/api/models/add':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            provider = data.get('provider', '')
            model_id = data.get('modelId', '')
            model_name = data.get('modelName', '')
            api_key = data.get('apiKey', '')
            role = data.get('role', 'fallback')
            
            if not verify_api_key(provider, api_key):
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "API Key validation failed"}).encode())
                return
            
            config = load_config()
            if "models" not in config: config["models"] = {"mode": "merge", "providers": {}}
            if "providers" not in config["models"]: config["models"]["providers"] = {}
            
            provider_base_urls = {
                "openrouter": "https://openrouter.ai/api/v1",
                "google": "https://generativelanguage.googleapis.com",
                "anthropic": "https://api.anthropic.com",
                "openai": "https://api.openai.com/v1",
                "minimax": "https://api.minimaxi.com/v1"
            }
            
            if provider not in config["models"]["providers"]:
                config["models"]["providers"][provider] = {
                    "baseUrl": provider_base_urls.get(provider, ""),
                    "api": "openai-completions" if provider not in ["anthropic", "google"] else "anthropic-messages",
                    "models": []
                }
            
            # Add model
            new_model = {
                "id": model_id,
                "name": model_name or model_id,
                "api": config["models"]["providers"][provider].get("api", "openai-completions")
            }
            
            # Check if model exists
            exists = any(m.get('id') == model_id for m in config["models"]["providers"][provider].get('models', []))
            if not exists:
                if "models" not in config["models"]["providers"][provider]:
                    config["models"]["providers"][provider]["models"] = []
                config["models"]["providers"][provider]["models"].append(new_model)
            
            # Add to agents defaults
            if "agents" not in config: config["agents"] = {}
            if "defaults" not in config["agents"]: config["agents"]["defaults"] = {}
            if "models" not in config["agents"]["defaults"]: config["agents"]["defaults"]["models"] = {}
            config["agents"]["defaults"]["models"][f"{provider}/{model_id}"] = {"alias": model_name or model_id}
            
            if role == "primary":
                config["agents"]["defaults"]["model"] = config["agents"]["defaults"].get("model", {})
                config["agents"]["defaults"]["model"]["primary"] = f"{provider}/{model_id}"
            else:  # fallback
                if "model" not in config["agents"]["defaults"]:
                    # Use the actual configured primary model as fallback base
                    current_primary = config.get("agents", {}).get("defaults", {}).get("model", {}).get("primary", "")
                    config["agents"]["defaults"]["model"] = {"primary": current_primary, "fallbacks": []}
                if "fallbacks" not in config["agents"]["defaults"]["model"]:
                    config["agents"]["defaults"]["model"]["fallbacks"] = []
                if f"{provider}/{model_id}" not in config["agents"]["defaults"]["model"]["fallbacks"]:
                    config["agents"]["defaults"]["model"]["fallbacks"].append(f"{provider}/{model_id}")

            save_config(config)

            # Also update agent-level auth-profiles.json and models.json
            # so the Gateway picks up the new model auth without manual restart
            try:
                agent_dir = os.path.expanduser('~/.openclaw/agents/main/agent')
                os.makedirs(agent_dir, exist_ok=True)

                # Update auth-profiles.json
                auth_path = os.path.join(agent_dir, 'auth-profiles.json')
                auth_data = {'version': 1, 'profiles': {}}
                if os.path.exists(auth_path):
                    with open(auth_path) as f:
                        auth_data = json.load(f)
                if 'profiles' not in auth_data:
                    auth_data['profiles'] = {}
                profile_key = f"{provider}:default"
                if api_key and profile_key not in auth_data['profiles']:
                    auth_data['profiles'][profile_key] = {
                        'type': 'api_key',
                        'provider': provider,
                        'key': api_key
                    }
                with open(auth_path, 'w') as f:
                    json.dump(auth_data, f, indent=2)

                # Update models.json
                models_path = os.path.join(agent_dir, 'models.json')
                models_data = {'providers': {}}
                if os.path.exists(models_path):
                    with open(models_path) as f:
                        models_data = json.load(f)
                if 'providers' not in models_data:
                    models_data['providers'] = {}
                provider_api = 'openai-completions' if provider not in ['anthropic', 'google'] else 'anthropic-messages'
                if provider not in models_data['providers']:
                    models_data['providers'][provider] = {
                        'baseUrl': provider_base_urls.get(provider, ''),
                        'api': provider_api,
                        'models': []
                    }
                existing_ids = [m.get('id') for m in models_data['providers'][provider].get('models', [])]
                if model_id not in existing_ids:
                    models_data['providers'][provider]['models'].append(new_model)
                if api_key:
                    models_data['providers'][provider]['apiKey'] = api_key
                with open(models_path, 'w') as f:
                    json.dump(models_data, f, indent=2)

                # Notify Gateway to reload
                subprocess.run(
                    ['openclaw', 'gateway', 'restart'],
                    capture_output=True, timeout=30
                )
            except Exception as e:
                print(f"Warning: failed to update agent auth files: {e}")

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode())
            return

        if self.path == '/api/models/delete':
            print("=== DELETE MODEL CALLED ===")
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            print(f"Delete request data: {data}")
            provider = data.get('provider', '')
            model_id = data.get('modelId', '')
            if provider == 'openrouter' and '/' in model_id:
                model_full_id = f"{provider}/{model_id}"
            elif '/' in model_id:
                short_id = model_id.rsplit('/', 1)[-1]
                model_full_id = f"{provider}/{short_id}"
            else:
                model_full_id = f"{provider}/{model_id}"
            
            if '/' in model_full_id:
                provider, model_id = model_full_id.split('/', 1)
                
                config = load_config()
                
                # Ensure nested structures exist
                if 'models' not in config: config['models'] = {}
                if 'providers' not in config['models']: config['models']['providers'] = {}
                if 'agents' not in config: config['agents'] = {}
                if 'defaults' not in config['agents']: config['agents']['defaults'] = {}
                if 'model' not in config['agents']['defaults']: config['agents']['defaults']['model'] = {'primary': '', 'fallbacks': []}
                if 'models' not in config['agents']['defaults']: config['agents']['defaults']['models'] = {}
                
                # Remove from providers
                if provider in config.get('models', {}).get('providers', {}):
                    models_list = config['models']['providers'][provider].get('models', [])
                    config['models']['providers'][provider]['models'] = [m for m in models_list if m.get('id') != model_id]
                
                # Remove from agents defaults models
                defaults_models = config.get('agents', {}).get('defaults', {}).get('models', {})
                if model_full_id in defaults_models:
                    del defaults_models[model_full_id]
                # Also remove any model that ends with the model_id
                for key in list(defaults_models.keys()):
                    if key.endswith('/' + model_id):
                        del defaults_models[key]
                
                # Remove from primary or fallbacks
                model_config = config.get('agents', {}).get('defaults', {}).get('model', {})
                if model_config.get('primary') == model_full_id:
                    model_config['primary'] = ''
                
                fallbacks = model_config.get('fallbacks', [])
                model_config['fallbacks'] = [m for m in fallbacks if m != model_full_id and not m.endswith('/' + model_id)]
                
                save_config(config)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode())
            return

        if self.path == '/api/models/set_primary':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            provider = data.get('provider', '')
            model_id = data.get('modelId', '')
            # For other providers, extract the last part
            # e.g., provider="google", modelId="models/gemini-2.5-flash" -> "google/gemini-2.5-flash"
            if provider == 'openrouter' and '/' in model_id:
                model_full_id = f"{provider}/{model_id}"
            elif '/' in model_id:
                short_id = model_id.rsplit('/', 1)[-1]
                model_full_id = f"{provider}/{short_id}"
            else:
                model_full_id = f"{provider}/{model_id}"
            
            config = load_config()
            if "agents" not in config: config["agents"] = {}
            if "defaults" not in config["agents"]: config["agents"]["defaults"] = {}
            if "model" not in config["agents"]["defaults"]: config["agents"]["defaults"]["model"] = {"fallbacks": []}
            
            # Remove from fallbacks if exists
            fallbacks = config["agents"]["defaults"]["model"].get("fallbacks", [])
            if model_full_id in fallbacks:
                fallbacks.remove(model_full_id)
            
            config["agents"]["defaults"]["model"]["primary"] = model_full_id
            save_config(config)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "success"}).encode())
            return

        # === Login API ===
        if self.path == '/login':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            
            username = data.get('username', '')
            password = data.get('password', '')
            
            # Verify user
            if not verify_user(username, password):
                self.send_response(401)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Invalid username or password"}).encode())
                return
            
            # Get token from gateway
            token = self.get_token()

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "token": token}).encode())
            return

        # === Chat API ===
        if self.path.startswith('/v1/'):
            cl = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(cl)
            gw = 'http://localhost:18789'
            try:
                if os.path.exists(CLAW):
                    c = json.load(open(CLAW))
                    gw = f"http://localhost:{c.get('gateway',{}).get('port',18789)}"
            except: pass
            req = urllib.request.Request(f"{gw}{self.path}", data=body,
                headers={'Content-Type': 'application/json', 'Authorization': self.headers.get('Authorization','')}, method='POST')
            try:
                r = urllib.request.urlopen(req, timeout=120)
                self.send_response(r.status)
                for h,v in r.getheaders():
                    if h.lower() in ['content-type','access-control-allow-origin']: self.send_header(h,v)
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(r.read())
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(e.read())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error':str(e)}).encode())
            return

        # === Move Fallback API ===
        if self.path == '/api/models/move_fallback':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            provider = data.get('provider', '')
            model_id = data.get('modelId', '')
            if provider == 'openrouter' and '/' in model_id:
                model_full_id = f"{provider}/{model_id}"
            elif '/' in model_id:
                short_id = model_id.rsplit('/', 1)[-1]
                model_full_id = f"{provider}/{short_id}"
            else:
                model_full_id = f"{provider}/{model_id}"
            direction = data.get('direction', '')  # 'up' or 'down'
            
            try:
                config = load_config()
                fallbacks = config.get('agents',{}).get('defaults',{}).get('model',{}).get('fallbacks', [])
                if model_full_id in fallbacks and direction in ['up', 'down']:
                    idx = fallbacks.index(model_full_id)
                    new_idx = idx - 1 if direction == 'up' else idx + 1
                    if 0 <= new_idx < len(fallbacks):
                        fallbacks[idx], fallbacks[new_idx] = fallbacks[new_idx], fallbacks[idx]
                        config['agents']['defaults']['model']['fallbacks'] = fallbacks
                        save_config(config)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        # === Set Fallback API ===
        if self.path == '/api/models/set_fallback':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            provider = data.get('provider', '')
            model_id = data.get('modelId', '')
            if provider == 'openrouter' and '/' in model_id:
                model_full_id = f"{provider}/{model_id}"
            elif '/' in model_id:
                short_id = model_id.rsplit('/', 1)[-1]
                model_full_id = f"{provider}/{short_id}"
            else:
                model_full_id = f"{provider}/{model_id}"
            
            try:
                config = load_config()
                fallbacks = config.get('agents',{}).get('defaults',{}).get('model',{}).get('fallbacks', [])
                
                # Remove if exists
                if model_full_id in fallbacks:
                    fallbacks.remove(model_full_id)
                
                # Add to end
                fallbacks.append(model_full_id)
                config['agents']['defaults']['model']['fallbacks'] = fallbacks
                save_config(config)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        # === Clear Role API ===
        if self.path == '/api/models/clear_role':
            cl = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(cl).decode())
            provider = data.get('provider', '')
            model_id = data.get('modelId', '')
            if provider == 'openrouter' and '/' in model_id:
                model_full_id = f"{provider}/{model_id}"
            elif '/' in model_id:
                short_id = model_id.rsplit('/', 1)[-1]
                model_full_id = f"{provider}/{short_id}"
            else:
                model_full_id = f"{provider}/{model_id}"
            
            try:
                config = load_config()
                
                # Remove from primary
                if config.get('agents',{}).get('defaults',{}).get('model',{}).get('primary') == model_full_id:
                    config['agents']['defaults']['model']['primary'] = ''
                
                # Remove from fallbacks
                fallbacks = config.get('agents',{}).get('defaults',{}).get('model',{}).get('fallbacks', [])
                if model_full_id in fallbacks:
                    fallbacks.remove(model_full_id)
                    config['agents']['defaults']['model']['fallbacks'] = fallbacks
                
                save_config(config)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True}).encode())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        # === Command Forwarding API (for OpenClaw commands) ===
        if self.path.startswith('/api/command'):
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode()) if cl > 0 else {}
            cmd = body.get('command', '')
            
            if not cmd:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'error': 'No command provided'}).encode())
                return
            
            auth = self.headers.get('Authorization', '').replace('Bearer ', '')
            if not auth:
                auth = self.get_token()
            
            gw_port = 18789
            try:
                if os.path.exists(CLAW):
                    c = json.load(open(CLAW))
                    gw_port = c.get('gateway',{}).get('port',18789)
            except: pass
            
            # Forward command to gateway
            req = urllib.request.Request(
                f"http://localhost:{gw_port}/v1/command",
                data=json.dumps({'command': cmd}).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {auth}'},
                method='POST'
            )
            
            try:
                r = urllib.request.urlopen(req, timeout=60)
                self.send_response(r.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(r.read())
            except urllib.error.HTTPError as e:
                self.send_response(e.code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(e.read())
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': str(e)}).encode())
            return

        # === Chat API ===
        if self.path == '/chat':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode())
            user_msg = body.get('message', '')
            session_id = body.get('session_id', 'default')
            model = body.get('model', self.get_primary_model())
            
            sessions = load_sessions()
            if session_id not in sessions:
                sessions[session_id] = {'messages': [], 'updated': 0}
            entry = sessions[session_id]
            # Compat: old format is a plain list
            if isinstance(entry, list):
                entry = {'messages': entry, 'updated': 0}
            history = entry.get('messages', [])

            messages = list(history)
            messages.append({'role': 'user', 'content': user_msg})
            
            auth = self.headers.get('Authorization', '').replace('Bearer ', '')
            if not auth:
                auth = self.get_token()
            
            gw_port = 18789
            try:
                if os.path.exists(CLAW):
                    c = json.load(open(CLAW))
                    gw_port = c.get('gateway',{}).get('port',18789)
            except: pass
            
            req = urllib.request.Request(
                f"http://localhost:{gw_port}/v1/chat/completions",
                data=json.dumps({'model': model, 'messages': messages}).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {auth}'},
                method='POST'
            )
            
            try:
                r = urllib.request.urlopen(req, timeout=1800)
                resp_data = json.loads(r.read().decode())
                assistant_msg = resp_data.get('choices', [{}])[0].get('message', {}).get('content', '')
                messages.append({'role': 'assistant', 'content': assistant_msg})
                sessions[session_id] = {'messages': messages[-20:], 'updated': time.time()}
                save_sessions(sessions)
                
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'response': assistant_msg, 'session_id': session_id}).encode())
            except Exception as e:
                error_msg = str(e)
                try:
                    if hasattr(e, 'read'):
                        error_body = e.read().decode()
                        error_msg += ' | ' + error_body
                except: pass
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({'error': error_msg}).encode())
            return
        
        if self.path == '/clear':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode())
            session_id = body.get('session_id', 'default')
            sessions = load_sessions()
            sessions[session_id] = {'messages': [], 'updated': time.time()}
            save_sessions(sessions)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True}).encode())
            return
        
        if self.path == '/delete_session':
            cl = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(cl).decode())
            session_id = body.get('session_id', '')
            sessions = load_sessions()
            if session_id in sessions:
                del sessions[session_id]
                save_sessions(sessions)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'success': True}).encode())
            return

        # Default: 404
        self.send_response(404)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'error': 'Not found'}).encode())

# ========== Server Startup ==========
if __name__ == '__main__':
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    print(f"WebChat with Model Manager & Channel Management running on port {PORT}...")
    ThreadingHTTPServer(('', PORT), H).serve_forever()
