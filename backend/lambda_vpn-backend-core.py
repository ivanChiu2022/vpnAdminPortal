import base64
import json
import os
import re
import time
from datetime import datetime

import boto3

# AWS clients
s3 = boto3.client("s3")
ssm = boto3.client("ssm")
ses = boto3.client("ses", region_name="us-east-1")

# =========================
# Environment Variables
# =========================
BUCKET_NAME = os.environ.get("BUCKET_NAME", "your S3 bucket name here")
USERS_PREFIX = os.environ.get("USERS_PREFIX", "your users prefix here, e.g. 'users/'")
CONFIGS_PREFIX = os.environ.get("CONFIGS_PREFIX", "your configs prefix here, e.g. 'configs/'")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "YOUR EC2 INSTANCE ID HERE")

WG_INTERFACE = os.environ.get("WG_INTERFACE", "wg0")
WG_SERVER_PUBLIC_KEY = os.environ.get(
    "WG_SERVER_PUBLIC_KEY",
    "your WireGuard server public key here"
)
WG_ENDPOINT = os.environ.get("WG_ENDPOINT", "your WireGuard endpoint here")
WG_DNS = os.environ.get("WG_DNS", "your WireGuard DNS here")

VPN_SUBNET_PREFIX = os.environ.get("VPN_SUBNET_PREFIX", "your internal vpn subnet prefix here, e.g. '10.0.0.'")
VPN_CLIENT_START = int(os.environ.get("VPN_CLIENT_START", "any number between 2 and 253, e.g. 2"))
VPN_CLIENT_END = int(os.environ.get("VPN_CLIENT_END", "any number between 2 and 253, e.g. 254"))

SSM_POLL_SECONDS = int(os.environ.get("SSM_POLL_SECONDS", "your SSM poll seconds here, e.g. 2"))
SSM_MAX_POLLS = int(os.environ.get("SSM_MAX_POLLS", "20"))

SES_SENDER = os.environ.get("SES_SENDER", "SES sender email here, e.g. 'sender@example.com'")
SES_RECEIVER = os.environ.get("SES_RECEIVER", "recever email here, e.g. 'receiver@example.com'")


# =========================
# Main Lambda Handler
# =========================
def lambda_handler(event, context):
    print("Raw event:", json.dumps(event))

    try:
        payload = parse_event_payload(event)
        print("Parsed payload:", payload)

        action = payload.get("action")

        if action == "create_user":
            username = payload.get("username")
            device = payload.get("device")
            result = create_user(username, device)
            return response(200, result)

        elif action == "list_users":
            result = list_users()
            return response(200, result)

        elif action == "delete_user":
            username = payload.get("username")
            result = delete_user(username)
            return response(200, result)

        elif action == "get_status":
            users = list_users()
            return response(200, {
                "message": "Status route is working",
                "total_users": users.get("count", 0),
                "users": users.get("users", [])
            })

        elif action == "test":
            send_plain_test_email()
            return response(200, {
                "message": "Test email sent successfully",
                "to": SES_RECEIVER
            })

        else:
            return response(400, {"message": "Invalid action"})

    except ValueError as e:
        print("Validation error:", str(e))
        return response(400, {"message": str(e)})

    except Exception as e:
        print("Unhandled error:", str(e))
        return response(500, {
            "message": "Internal server error",
            "error": str(e)
        })


# =========================
# Main Actions
# =========================
def create_user(username, device):
    validate_environment()
    validate_username(username)
    validate_device(device)

    users = get_existing_users()

    for user in users:
        if user.get("username") == username:
            raise ValueError(f"Username already exists: {username}")

    client_ip = find_next_available_ip(users)

    ssm_result = generate_keys_and_add_peer_via_ssm(
        username=username,
        client_ip=client_ip
    )

    client_private_key = ssm_result["client_private_key"]
    client_public_key = ssm_result["client_public_key"]

    client_config = build_client_config(
        client_private_key=client_private_key,
        client_ip=client_ip
    )

    user_record = {
        "username": username,
        "device": device,
        "ip": client_ip,
        "public_key": client_public_key,
        "created_at": utc_now_iso(),
        "status": "active",
        "wg_interface": WG_INTERFACE
    }

    save_user_record(user_record)
    save_config_file(username, client_config)
    send_conf_email(username, device, client_ip, client_config)

    return {
        "message": "User created successfully",
        "username": username,
        "device": device,
        "ip": client_ip,
        "status": "active",
        "json_file": f"{USERS_PREFIX}{username}.json",
        "conf_file": f"{CONFIGS_PREFIX}{username}.conf",
        "email_sent_to": SES_RECEIVER
    }


def list_users():
    raw_users = get_existing_users()
    normalized_users = []

    for user in raw_users:
        normalized_users.append({
            "username": user.get("username", ""),
            "device": user.get("device") or user.get("device_type", "unknown"),
            "ip": user.get("ip") or user.get("vpn_ip", ""),
            "status": user.get("status", ""),
            "created_at": user.get("created_at", "")
        })

    users_sorted = sorted(normalized_users, key=lambda x: x.get("username", ""))

    return {
        "count": len(users_sorted),
        "users": users_sorted
    }


def delete_user(username):
    validate_environment()
    validate_username(username)

    print(f"Delete user: {username}")

    users = get_existing_users()
    user = next((u for u in users if u.get("username") == username), None)

    if not user:
        raise ValueError(f"User not found: {username}")

    public_key = user.get("public_key")
    if not public_key:
        raise ValueError(f"Missing public_key for user: {username}")

    ssm_result = remove_peer_via_ssm(username=username, public_key=public_key)
    command_id = ssm_result["command_id"]

    delete_user_record(username)
    delete_config_file(username)

    return {
        "message": f"{username} deleted successfully",
        "command_id": command_id
    }


# =========================
# Validation Functions
# =========================
def validate_environment():
    missing = []

    if not BUCKET_NAME:
        missing.append("BUCKET_NAME")
    if not USERS_PREFIX:
        missing.append("USERS_PREFIX")
    if not CONFIGS_PREFIX:
        missing.append("CONFIGS_PREFIX")
    if not INSTANCE_ID:
        missing.append("INSTANCE_ID")
    if not WG_SERVER_PUBLIC_KEY:
        missing.append("WG_SERVER_PUBLIC_KEY")
    if not WG_ENDPOINT:
        missing.append("WG_ENDPOINT")
    if not SES_SENDER:
        missing.append("SES_SENDER")
    if not SES_RECEIVER:
        missing.append("SES_RECEIVER")

    if missing:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def validate_username(username):
    if not username:
        raise ValueError("Missing username")

    if not re.fullmatch(r"[A-Za-z0-9_-]{3,50}", username):
        raise ValueError(
            "Invalid username. Use 3-50 characters: letters, numbers, dash, underscore only."
        )


def validate_device(device):
    if not device:
        raise ValueError("Missing device")

    if device not in ["laptop", "phone"]:
        raise ValueError("Invalid device. Allowed values: laptop, phone")


# =========================
# Event / Response Helpers
# =========================
def parse_event_payload(event):
    if not isinstance(event, dict):
        raise ValueError("Invalid event format")

    if "body" not in event:
        return event

    body = event.get("body")

    if body is None:
        return {}

    if event.get("isBase64Encoded") is True:
        body = base64.b64decode(body).decode("utf-8")

    if isinstance(body, str):
        body = body.strip()
        if not body:
            return {}
        return json.loads(body)

    if isinstance(body, dict):
        return body

    raise ValueError("Unsupported body format")


def response(status_code, body_dict):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body_dict)
    }


def utc_now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


# =========================
# S3 Functions
# =========================
def get_existing_users():
    users = []
    continuation_token = None

    while True:
        kwargs = {
            "Bucket": BUCKET_NAME,
            "Prefix": USERS_PREFIX
        }

        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        resp = s3.list_objects_v2(**kwargs)

        for obj in resp.get("Contents", []):
            key = obj["Key"]

            if not key.endswith(".json"):
                continue

            file_obj = s3.get_object(Bucket=BUCKET_NAME, Key=key)
            content = file_obj["Body"].read().decode("utf-8")
            user_data = json.loads(content)
            users.append(user_data)

        if resp.get("IsTruncated"):
            continuation_token = resp.get("NextContinuationToken")
        else:
            break

    return users


def save_user_record(user_record):
    key = f"{USERS_PREFIX}{user_record['username']}.json"

    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(user_record, indent=2).encode("utf-8"),
        ContentType="application/json"
    )


def delete_user_record(username):
    key = f"{USERS_PREFIX}{username}.json"
    s3.delete_object(Bucket=BUCKET_NAME, Key=key)
    print(f"Deleted user record from S3: {key}")


def save_config_file(username, client_config):
    key = f"{CONFIGS_PREFIX}{username}.conf"

    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=client_config.encode("utf-8"),
        ContentType="text/plain"
    )

    print(f"Saved config file to S3: {key}")


def delete_config_file(username):
    key = f"{CONFIGS_PREFIX}{username}.conf"

    try:
        s3.delete_object(Bucket=BUCKET_NAME, Key=key)
        print(f"Deleted config file from S3: {key}")
    except Exception as e:
        print(f"Delete config file warning: {str(e)}")


# =========================
# IP Management
# =========================
def find_next_available_ip(users):
    used_ips = set()

    for user in users:
        ip = user.get("ip") or user.get("vpn_ip")
        if ip:
            used_ips.add(ip)

    for host in range(VPN_CLIENT_START, VPN_CLIENT_END + 1):
        candidate = f"{VPN_SUBNET_PREFIX}{host}"
        if candidate not in used_ips:
            return candidate

    raise Exception("No available VPN IP addresses left")


# =========================
# WireGuard Client Config Builder
# =========================
def build_client_config(client_private_key, client_ip):
    return f"""[Interface]
PrivateKey = {client_private_key}
Address = {client_ip}/32
DNS = {WG_DNS}

[Peer]
PublicKey = {WG_SERVER_PUBLIC_KEY}
Endpoint = {WG_ENDPOINT}
AllowedIPs = 0.0.0.0/0, ::/0
PersistentKeepalive = 25
"""


# =========================
# SES Email Functions
# =========================
def send_plain_test_email():
    ses.send_email(
        Source=SES_SENDER,
        Destination={
            "ToAddresses": [SES_RECEIVER]
        },
        Message={
            "Subject": {
                "Data": "VPN Test Email"
            },
            "Body": {
                "Text": {
                    "Data": "This is a test email from your VPN system."
                }
            }
        }
    )


def send_conf_email(username, device, client_ip, client_config):
    subject = f"VPN Configuration File - {username}"

    body_text = f"""Hello,

A new VPN user profile has been generated.

Username: {username}
Device: {device}
VPN IP: {client_ip}

The WireGuard configuration file is attached to this email.

You can review it and forward it to the end user later.

Sender: {SES_SENDER}

Regards,
ivanVPN
"""

    raw_email = build_raw_email_with_attachment(
        sender=SES_SENDER,
        receiver=SES_RECEIVER,
        subject=subject,
        body_text=body_text,
        attachment_filename=f"{username}.conf",
        attachment_content=client_config,
        attachment_content_type="text/plain"
    )

    ses.send_raw_email(
        Source=SES_SENDER,
        Destinations=[SES_RECEIVER],
        RawMessage={"Data": raw_email}
    )

    print(f"Sent config email to {SES_RECEIVER}")


def build_raw_email_with_attachment(
    sender,
    receiver,
    subject,
    body_text,
    attachment_filename,
    attachment_content,
    attachment_content_type="text/plain"
):
    boundary = "NextPartBoundary"

    attachment_b64 = base64.b64encode(
        attachment_content.encode("utf-8")
    ).decode("utf-8")

    raw_email = f"""From: {sender}
To: {receiver}
Subject: {subject}
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="{boundary}"

--{boundary}
Content-Type: text/plain; charset="UTF-8"
Content-Transfer-Encoding: 7bit

{body_text}

--{boundary}
Content-Type: {attachment_content_type}; name="{attachment_filename}"
Content-Disposition: attachment; filename="{attachment_filename}"
Content-Transfer-Encoding: base64

{attachment_b64}

--{boundary}--
"""
    return raw_email.encode("utf-8")


# =========================
# SSM + WireGuard Peer Update
# =========================
def generate_keys_and_add_peer_via_ssm(username, client_ip):
    script = f"""
set -e

WG_INTERFACE="{WG_INTERFACE}"
WG_CONFIG="/etc/wireguard/${{WG_INTERFACE}}.conf"
USERNAME="{username}"
CLIENT_IP="{client_ip}"

if [ ! -f "$WG_CONFIG" ]; then
  echo '{{"error":"WireGuard config file not found","path":"'"$WG_CONFIG"'"}}'
  exit 1
fi

CLIENT_PRIVATE_KEY=$(wg genkey)
CLIENT_PUBLIC_KEY=$(printf '%s' "$CLIENT_PRIVATE_KEY" | wg pubkey)

cat >> "$WG_CONFIG" <<EOF

# managed-by-lambda:{username}
[Peer]
PublicKey = $CLIENT_PUBLIC_KEY
AllowedIPs = $CLIENT_IP/32
EOF

systemctl restart wg-quick@${{WG_INTERFACE}}

printf '{{"client_private_key":"%s","client_public_key":"%s"}}' "$CLIENT_PRIVATE_KEY" "$CLIENT_PUBLIC_KEY"
"""

    command_id = send_ssm_script(script, comment="vpn create_user add peer")
    result = wait_for_ssm_command(command_id)

    stdout = result.get("StandardOutputContent", "").strip()
    stderr = result.get("StandardErrorContent", "").strip()

    print("SSM stdout:", stdout)
    print("SSM stderr:", stderr)

    if result.get("Status") != "Success":
        raise Exception(
            f"SSM command failed. Status={result.get('Status')}. Error={stderr or stdout}"
        )

    parsed = extract_json_from_text(stdout)
    if not parsed:
        raise Exception(f"Could not parse key output from SSM. Raw output: {stdout}")

    if "error" in parsed:
        raise Exception(parsed["error"])

    if "client_private_key" not in parsed or "client_public_key" not in parsed:
        raise Exception(f"Missing keys in SSM output: {parsed}")

    return parsed


def remove_peer_via_ssm(username, public_key):
    script = f"""
set -e

WG_INTERFACE="{WG_INTERFACE}"
WG_CONFIG="/etc/wireguard/${{WG_INTERFACE}}.conf"
USERNAME="{username}"
PUBLIC_KEY="{public_key}"
TMP_FILE="/tmp/${{WG_INTERFACE}}.conf.cleaned"

if [ ! -f "$WG_CONFIG" ]; then
  echo '{{"error":"WireGuard config file not found","path":"'"$WG_CONFIG"'"}}'
  exit 1
fi

cp "$WG_CONFIG" "${{WG_CONFIG}}.bak"

awk -v username="{username}" '
BEGIN {{
    skip = 0
}}
$0 == "# managed-by-lambda:" username {{
    skip = 1
    next
}}
skip && /^\\[Peer\\]$/ {{
    next
}}
skip && /^PublicKey = / {{
    next
}}
skip && /^AllowedIPs = / {{
    skip = 0
    next
}}
!skip {{
    print
}}
' "$WG_CONFIG" > "$TMP_FILE"

mv "$TMP_FILE" "$WG_CONFIG"

wg set "$WG_INTERFACE" peer "$PUBLIC_KEY" remove || true
systemctl restart wg-quick@"$WG_INTERFACE"
wg show "$WG_INTERFACE"
"""

    command_id = send_ssm_script(
        script,
        comment=f"vpn delete_user remove peer {username}"
    )
    result = wait_for_ssm_command(command_id)

    stdout = result.get("StandardOutputContent", "").strip()
    stderr = result.get("StandardErrorContent", "").strip()

    print("Delete SSM stdout:", stdout)
    print("Delete SSM stderr:", stderr)

    if result.get("Status") != "Success":
        raise Exception(
            f"Delete SSM command failed. Status={result.get('Status')}. Error={stderr or stdout}"
        )

    return {
        "command_id": command_id,
        "stdout": stdout,
        "stderr": stderr
    }


def send_ssm_script(script_text, comment="vpn ssm command"):
    ssm_response = ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script_text]},
        Comment=comment
    )

    command_id = ssm_response["Command"]["CommandId"]
    print("SSM command sent:", command_id)
    return command_id


def wait_for_ssm_command(command_id):
    for _ in range(SSM_MAX_POLLS):
        time.sleep(SSM_POLL_SECONDS)

        result = ssm.get_command_invocation(
            CommandId=command_id,
            InstanceId=INSTANCE_ID
        )

        status = result.get("Status")
        print("SSM poll status:", status)

        if status in ["Success", "Cancelled", "TimedOut", "Failed", "Cancelling"]:
            return result

    raise Exception("Timed out waiting for SSM command result")


def extract_json_from_text(text):
    if not text:
        return None

    text = text.strip()

    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except Exception:
            return None

    return None