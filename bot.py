import os
import sys
import logging
import time
import json
import threading
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
import telebot
from telebot import types
import io
import re
import qrcode
import requests
from io import BytesIO
import base64

# ==================== CONFIG ====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = []
DATABASE_PATH = 'bot_database.db'
PORT = int(os.getenv('PORT', 8080))

# Cashfree Payment Gateway (server-to-server UPI QR)
CASHFREE_CLIENT_ID = os.getenv('CASHFREE_CLIENT_ID', '1379616996680a8580f65244ee76169731').strip()
CASHFREE_CLIENT_SECRET = os.getenv('CASHFREE_CLIENT_SECRET', 'cfsk_ma_prod_2ffb84f77c4586513e32bb8b13ed412b_e8070736').strip()
CASHFREE_API_VERSION = os.getenv('CASHFREE_API_VERSION', '2023-08-01').strip()
CASHFREE_BASE_URL = os.getenv('CASHFREE_BASE_URL', 'https://api.cashfree.com').rstrip('/')

if not BOT_TOKEN or BOT_TOKEN in {"YOUR_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE"}:
    print("❌ BOT_TOKEN Railway Variables me set karo!")
    sys.exit(1)

# ==================== LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== CASHFREE QR ====================
def cashfree_enabled():
    return bool(CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET)

def create_cashfree_upi_qr(user_id, plan):
    """Create a Cashfree order and initiate a server-to-server UPI QR.

    IMPORTANT: Cashfree must enable the S2S/Order Pay flag for this merchant.
    This function only creates the QR/payment transaction. It does NOT
    auto-approve the bot's manual verification flow.
    """
    if not cashfree_enabled():
        return None, None, None, 'Cashfree credentials are not configured.'

    order_id = f"tg_{user_id}_{plan['plan_id']}_{int(time.time())}"
    customer = {
        'customer_id': str(user_id),
        'customer_phone': '9999999999',
    }
    headers = {
        'x-client-id': CASHFREE_CLIENT_ID,
        'x-client-secret': CASHFREE_CLIENT_SECRET,
        'x-api-version': CASHFREE_API_VERSION,
        'x-request-id': f"tg-{user_id}-{int(time.time())}",
        'x-idempotency-key': str(__import__('uuid').uuid4()),
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    payload = {
        'order_id': order_id,
        'order_amount': float(plan['price']),
        'order_currency': 'INR',
        'customer_details': customer,
    }

    try:
        # 1) Create Cashfree order.
        r = requests.post(
            f'{CASHFREE_BASE_URL}/pg/orders',
            headers=headers,
            json=payload,
            timeout=20,
        )
        if r.status_code >= 300:
            logger.error('Cashfree Create Order %s: %s', r.status_code, r.text[:1000])
            return None, None, None, f'Cashfree order error {r.status_code}: {r.text[:500]}'

        order = r.json()
        session_id = order.get('payment_session_id')
        if not session_id:
            return None, order_id, None, 'Cashfree did not return payment_session_id.'

        # 2) Initiate UPI QR using Order Pay (/orders/sessions).
        # Cashfree documents `upi.channel=qrcode` for UPI QR.
        qr_headers = dict(headers)
        qr_headers['x-idempotency-key'] = str(__import__('uuid').uuid4())
        qr_payload = {
            'payment_session_id': session_id,
            'payment_method': {
                'upi': {
                    'channel': 'qrcode'
                }
            }
        }
        r2 = requests.post(
            f'{CASHFREE_BASE_URL}/pg/orders/sessions',
            headers=qr_headers,
            json=qr_payload,
            timeout=20,
        )
        if r2.status_code >= 300:
            logger.error('Cashfree Order Pay/QR %s: %s', r2.status_code, r2.text[:1500])
            hint = ''
            if r2.status_code in (400, 401, 403, 404):
                hint = ' Check that S2S/UPI QR is enabled for this Cashfree merchant account.'
            return None, order_id, None, f'Cashfree QR error {r2.status_code}: {r2.text[:700]}{hint}'

        pay = r2.json()
        cf_payment_id = str(pay.get('cf_payment_id') or '')

        # Cashfree returns QR image data under data.payload.qrcode.
        data = pay.get('data') or {}
        payload_data = data.get('payload') or {}
        qr_value = (
            payload_data.get('qrcode')
            or data.get('qrcode')
            or pay.get('qrcode')
        )

        if not qr_value:
            logger.error('Cashfree QR response did not contain qrcode: %s', json.dumps(pay)[:2000])
            return None, order_id, cf_payment_id, (
                'Cashfree accepted the order but returned no QR image. '
                'Ask Cashfree to enable S2S UPI QR/Order Pay for this account.'
            )

        # Cashfree normally returns data:image/png;base64,... .
        if isinstance(qr_value, str) and qr_value.startswith('data:image') and ',' in qr_value:
            qr_value = qr_value.split(',', 1)[1]

        try:
            image = BytesIO(base64.b64decode(qr_value, validate=True))
            image.seek(0)
            return image, order_id, cf_payment_id, None
        except Exception:
            # If Cashfree returns a UPI URI instead of an image, render it locally.
            image = generate_upi_qr(str(qr_value), plan['price'], plan['name'])
            if image:
                return image, order_id, cf_payment_id, None
            return None, order_id, cf_payment_id, 'Cashfree returned an unsupported QR format.'

    except requests.RequestException as e:
        logger.exception('Cashfree connection error')
        return None, order_id, None, f'Cashfree connection error: {e}'
    except Exception as e:
        logger.exception('Cashfree QR error')
        return None, order_id, None, str(e)

# ==================== QR GENERATOR ====================
def generate_upi_qr(upi_id, amount, plan_name):
    """Generate UPI QR code automatically"""
    try:
        upi_string = f"upi://pay?pa={upi_id}&pn={plan_name}&am={amount}&cu=INR"
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(upi_string)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        img_bytes = BytesIO()
        img.save(img_bytes, format='PNG')
        img_bytes.seek(0)
        
        return img_bytes
    except Exception as e:
        logger.error(f"QR generation error: {e}")
        return None

# ==================== DATABASE ====================
class Database:
    def __init__(self):
        self.db_path = DATABASE_PATH
        self.init_tables()
    
    def get_conn(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)
    
    def init_tables(self):
        conn = self.get_conn()
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            subscription_plan_id INTEGER,
            subscription_expiry TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_admin INTEGER DEFAULT 0
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            validity_days INTEGER NOT NULL,
            channel_link TEXT,
            description TEXT,
            media_json TEXT DEFAULT '[]',
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            screenshot_file_id TEXT,
            status TEXT DEFAULT 'pending',
            admin_comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT
        )''')
        
        # Safe migrations for older bot databases.
        for column, definition in [
            ('cf_order_id', 'TEXT'),
            ('cf_payment_id', 'TEXT'),
        ]:
            try:
                c.execute(f'ALTER TABLE payments ADD COLUMN {column} {definition}')
            except sqlite3.OperationalError:
                pass

        c.execute('''CREATE TABLE IF NOT EXISTS settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS welcome_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL,
            order_num INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')
        
        defaults = [
            ('welcome_image', ''),
            ('welcome_text', 'Welcome to Premium Bot! 🎉\n\nGet exclusive access to premium content\nAffordable plans starting at just ₹0'),
            ('bot_name', 'PREMIUM BOT'),
            ('upi_id', ''),
            ('welcome_video', ''),
            ('proof_media_json', '[]'),
            ('proof_description', '🔥 PAYMENT PROOF / VERIFIED PURCHASE\n\nNew payment approved successfully!'),
            ('proof_channel_id', ''),
            ('proof_channel_link', '')
        ]
        for key, val in defaults:
            c.execute('INSERT OR IGNORE INTO settings (setting_key, setting_value) VALUES (?, ?)', (key, val))
        
        conn.commit()
        conn.close()
        logger.info("✅ Database ready")
    
    # ==================== WELCOME VIDEOS METHODS ====================
    def add_welcome_video(self, file_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM welcome_videos')
        count = c.fetchone()[0]
        if count >= 5:
            conn.close()
            return False, "Maximum 5 videos allowed! Delete some first."
        c.execute('INSERT INTO welcome_videos (file_id, order_num) VALUES (?, ?)', (file_id, count + 1))
        conn.commit()
        conn.close()
        return True, f"✅ Video {count + 1}/5 added!"
    
    def get_welcome_videos(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM welcome_videos ORDER BY order_num ASC')
        rows = c.fetchall()
        conn.close()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in rows]
    
    def delete_welcome_video(self, video_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('DELETE FROM welcome_videos WHERE id = ?', (video_id,))
        conn.commit()
        conn.close()
    
    def clear_welcome_videos(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('DELETE FROM welcome_videos')
        conn.commit()
        conn.close()
    
    def export_database(self):
        """Export only Users and Plans (with media) data as JSON"""
        conn = self.get_conn()
        c = conn.cursor()
        
        export_data = {}
        
        c.execute('SELECT * FROM users')
        rows = c.fetchall()
        if rows:
            cols = [d[0] for d in c.description]
            export_data['users'] = [dict(zip(cols, row)) for row in rows]
        else:
            export_data['users'] = []
        
        c.execute('SELECT * FROM plans WHERE is_active = 1')
        rows = c.fetchall()
        if rows:
            cols = [d[0] for d in c.description]
            plans = [dict(zip(cols, row)) for row in rows]
            for plan in plans:
                if 'media_json' not in plan:
                    plan['media_json'] = '[]'
            export_data['plans'] = plans
        else:
            export_data['plans'] = []
        
        # Export proof settings
        export_data['proof_settings'] = {
            'proof_media_json': self.get_setting('proof_media_json'),
            'proof_description': self.get_setting('proof_description'),
            'proof_channel_id': self.get_setting('proof_channel_id'),
            'proof_channel_link': self.get_setting('proof_channel_link')
        }

        # Export welcome videos
        c.execute('SELECT * FROM welcome_videos')
        rows = c.fetchall()
        if rows:
            cols = [d[0] for d in c.description]
            export_data['welcome_videos'] = [dict(zip(cols, row)) for row in rows]
        else:
            export_data['welcome_videos'] = []
        
        conn.close()
        return export_data
    
    def import_database(self, data):
        """Import Users and Plans (with media) from JSON"""
        conn = self.get_conn()
        c = conn.cursor()
        
        try:
            c.execute("PRAGMA foreign_keys = OFF")
            
            c.execute("DELETE FROM users")
            c.execute("DELETE FROM plans")
            c.execute("DELETE FROM welcome_videos")
            
            if 'users' in data and data['users']:
                for user in data['users']:
                    c.execute('''INSERT OR REPLACE INTO users 
                        (user_id, username, first_name, last_name, subscription_plan_id, 
                         subscription_expiry, created_at, is_admin) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (user.get('user_id'), user.get('username', ''), user.get('first_name', ''),
                         user.get('last_name', ''), user.get('subscription_plan_id'),
                         user.get('subscription_expiry'), user.get('created_at', datetime.now().isoformat()),
                         user.get('is_admin', 0)))
            
            if 'plans' in data and data['plans']:
                for plan in data['plans']:
                    c.execute('''INSERT OR REPLACE INTO plans 
                        (plan_id, name, price, validity_days, channel_link, description, 
                         media_json, is_active, created_at) 
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                        (plan.get('plan_id'), plan.get('name'), plan.get('price'),
                         plan.get('validity_days'), plan.get('channel_link', ''),
                         plan.get('description', ''), plan.get('media_json', '[]'),
                         plan.get('is_active', 1), plan.get('created_at', datetime.now().isoformat())))
            
            if 'proof_settings' in data:
                for key, value in data['proof_settings'].items():
                    c.execute('INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)', (key, value or ''))

            if 'welcome_videos' in data and data['welcome_videos']:
                for video in data['welcome_videos']:
                    c.execute('''INSERT OR REPLACE INTO welcome_videos 
                        (id, file_id, order_num, created_at) 
                        VALUES (?, ?, ?, ?)''',
                        (video.get('id'), video.get('file_id'), video.get('order_num'),
                         video.get('created_at', datetime.now().isoformat())))
            
            c.execute("PRAGMA foreign_keys = ON")
            conn.commit()
            conn.close()
            return True, None
            
        except Exception as e:
            logger.error(f"Import error: {e}")
            conn.close()
            return False, str(e)
    
    def add_user(self, user_id, username='', first_name='', last_name=''):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO users (user_id, username, first_name, last_name) VALUES (?, ?, ?, ?)',
                 (user_id, username, first_name, last_name))
        conn.commit()
        conn.close()
    
    def get_user(self, user_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        if row:
            cols = [d[0] for d in c.description]
            return dict(zip(cols, row))
        return None
    
    def get_all_users(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM users ORDER BY created_at DESC')
        rows = c.fetchall()
        conn.close()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in rows]
    
    def set_admin(self, user_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('UPDATE users SET is_admin = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
    
    def is_admin(self, user_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT is_admin FROM users WHERE user_id = ?', (user_id,))
        row = c.fetchone()
        conn.close()
        return row and row[0] == 1
    
    def update_subscription(self, user_id, plan_id, days):
        conn = self.get_conn()
        c = conn.cursor()
        expiry = (datetime.now() + timedelta(days=days)).isoformat()
        c.execute('UPDATE users SET subscription_plan_id = ?, subscription_expiry = ? WHERE user_id = ?',
                 (plan_id, expiry, user_id))
        conn.commit()
        conn.close()
        return expiry
    
    def add_plan(self, name, price, days, channel_link, description=''):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('INSERT INTO plans (name, price, validity_days, channel_link, description) VALUES (?, ?, ?, ?, ?)',
                 (name, price, days, channel_link, description))
        plan_id = c.lastrowid
        conn.commit()
        conn.close()
        return plan_id
    
    def get_plan(self, plan_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM plans WHERE plan_id = ? AND is_active = 1', (plan_id,))
        row = c.fetchone()
        conn.close()
        if row:
            cols = [d[0] for d in c.description]
            return dict(zip(cols, row))
        return None
    
    def get_all_plans(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM plans WHERE is_active = 1 ORDER BY price ASC')
        rows = c.fetchall()
        conn.close()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in rows]
    
    def update_plan(self, plan_id, **kwargs):
        conn = self.get_conn()
        c = conn.cursor()
        allowed = ['name', 'price', 'validity_days', 'channel_link', 'description', 'media_json']
        updates = []
        vals = []
        for k, v in kwargs.items():
            if k in allowed:
                updates.append(f"{k} = ?")
                vals.append(v)
        if updates:
            vals.append(plan_id)
            c.execute(f"UPDATE plans SET {', '.join(updates)} WHERE plan_id = ?", vals)
            conn.commit()
        conn.close()
    
    def delete_plan(self, plan_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('UPDATE plans SET is_active = 0 WHERE plan_id = ?', (plan_id,))
        conn.commit()
        conn.close()
    
    def add_media(self, plan_id, media_type, file_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT media_json FROM plans WHERE plan_id = ?', (plan_id,))
        row = c.fetchone()
        if row:
            media_list = json.loads(row[0]) if row[0] else []
            media_list.append({'type': media_type, 'file_id': file_id, 'added_at': datetime.now().isoformat()})
            c.execute('UPDATE plans SET media_json = ? WHERE plan_id = ?', (json.dumps(media_list), plan_id))
            conn.commit()
        conn.close()
    
    def get_plan_media(self, plan_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT media_json FROM plans WHERE plan_id = ?', (plan_id,))
        row = c.fetchone()
        conn.close()
        if row:
            return json.loads(row[0]) if row[0] else []
        return []
    
    def add_payment(self, user_id, plan_id, amount, file_id, cf_order_id='', cf_payment_id=''):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('''INSERT INTO payments
            (user_id, plan_id, amount, screenshot_file_id, status, cf_order_id, cf_payment_id)
            VALUES (?, ?, ?, ?, "pending", ?, ?)''',
                 (user_id, plan_id, amount, file_id, cf_order_id, cf_payment_id))
        payment_id = c.lastrowid
        conn.commit()
        conn.close()
        return payment_id
    
    def get_payment(self, payment_id):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM payments WHERE payment_id = ?', (payment_id,))
        row = c.fetchone()
        conn.close()
        if row:
            cols = [d[0] for d in c.description]
            return dict(zip(cols, row))
        return None
    
    def get_pending_payments(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('''
            SELECT p.*, u.username, u.first_name, u.last_name, pl.name as plan_name
            FROM payments p
            JOIN users u ON p.user_id = u.user_id
            JOIN plans pl ON p.plan_id = pl.plan_id
            WHERE p.status = 'pending'
            ORDER BY p.created_at ASC
        ''')
        rows = c.fetchall()
        conn.close()
        cols = [d[0] for d in c.description]
        return [dict(zip(cols, row)) for row in rows]
    
    def approve_payment(self, payment_id):
        conn = self.get_conn()
        c = conn.cursor()
        
        try:
            c.execute('SELECT status FROM payments WHERE payment_id = ?', (payment_id,))
            row = c.fetchone()
            if not row:
                conn.close()
                return False, "Payment not found"
            if row[0] != 'pending':
                conn.close()
                return False, f"Payment already {row[0]}"
            
            now = datetime.now().isoformat()
            c.execute('UPDATE payments SET status = "approved", updated_at = ?, approved_at = ? WHERE payment_id = ?',
                     (now, now, payment_id))
            
            c.execute('SELECT user_id, plan_id FROM payments WHERE payment_id = ?', (payment_id,))
            payment = c.fetchone()
            
            if payment:
                user_id, plan_id = payment
                plan = self.get_plan(plan_id)
                if plan:
                    expiry = (datetime.now() + timedelta(days=plan['validity_days'])).isoformat()
                    c.execute('UPDATE users SET subscription_plan_id = ?, subscription_expiry = ? WHERE user_id = ?',
                             (plan_id, expiry, user_id))
            
            conn.commit()
            conn.close()
            return True, None
            
        except Exception as e:
            logger.error(f"Approve payment error: {e}")
            conn.close()
            return False, str(e)
    
    def reject_payment(self, payment_id, reason=""):
        conn = self.get_conn()
        c = conn.cursor()
        
        try:
            c.execute('SELECT status FROM payments WHERE payment_id = ?', (payment_id,))
            row = c.fetchone()
            if not row:
                conn.close()
                return False, "Payment not found"
            if row[0] != 'pending':
                conn.close()
                return False, f"Payment already {row[0]}"
            
            c.execute('UPDATE payments SET status = "rejected", admin_comment = ?, updated_at = CURRENT_TIMESTAMP WHERE payment_id = ?',
                     (reason, payment_id))
            
            conn.commit()
            conn.close()
            return True, None
            
        except Exception as e:
            logger.error(f"Reject payment error: {e}")
            conn.close()
            return False, str(e)
    
    def get_proof_media(self):
        try:
            value = self.get_setting('proof_media_json')
            return json.loads(value) if value else []
        except Exception:
            return []

    def add_proof_media(self, media_type, file_id, caption='', buy_url=''):
        """Add a proof to the rolling 4-item list. Newest proof is kept; oldest is removed."""
        media = self.get_proof_media()
        item = {
            'type': media_type,
            'file_id': file_id,
            'caption': caption or '',
            'buy_url': buy_url or '',
            'added_at': datetime.now().isoformat()
        }
        media.append(item)
        # Keep only the newest four proofs.
        if len(media) > 4:
            media = media[-4:]
        self.set_setting('proof_media_json', json.dumps(media))
        return True, f'Proof {len(media)}/4 added successfully.'

    def clear_proof_media(self):
        self.set_setting('proof_media_json', '[]')

    def get_admin_users(self):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE is_admin = 1 ORDER BY created_at ASC')
        rows = c.fetchall()
        cols = [d[0] for d in c.description]
        conn.close()
        return [dict(zip(cols, row)) for row in rows]

    def get_setting(self, key):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('SELECT setting_value FROM settings WHERE setting_key = ?', (key,))
        row = c.fetchone()
        conn.close()
        return row[0] if row else ''
    
    def set_setting(self, key, value):
        conn = self.get_conn()
        c = conn.cursor()
        c.execute('INSERT OR REPLACE INTO settings (setting_key, setting_value) VALUES (?, ?)', (key, value))
        conn.commit()
        conn.close()
    
    def get_stats(self):
        conn = self.get_conn()
        c = conn.cursor()
        stats = {}
        c.execute('SELECT COUNT(*) FROM users')
        stats['users'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM plans WHERE is_active = 1')
        stats['plans'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM payments WHERE status = "pending"')
        stats['pending'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM payments WHERE status = "approved"')
        stats['approved'] = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM payments WHERE status = "rejected"')
        stats['rejected'] = c.fetchone()[0]
        conn.close()
        return stats
    
    # ==================== EARNING FUNCTIONS ====================
    def get_earning_stats(self):
        """Get complete earning statistics"""
        conn = self.get_conn()
        c = conn.cursor()
        
        stats = {
            'total_earnings': 0,
            'today_earnings': 0,
            'this_week': 0,
            'this_month': 0,
            'plan_wise': {},
            'today_count': 0,
            'total_count': 0
        }
        
        today = datetime.now().date().isoformat()
        week_start = (datetime.now() - timedelta(days=7)).isoformat()
        month_start = (datetime.now() - timedelta(days=30)).isoformat()
        
        # Total approved payments
        c.execute('SELECT SUM(amount), COUNT(*) FROM payments WHERE status = "approved"')
        row = c.fetchone()
        stats['total_earnings'] = row[0] or 0
        stats['total_count'] = row[1] or 0
        
        # Today's earnings
        c.execute('SELECT SUM(amount), COUNT(*) FROM payments WHERE status = "approved" AND date(approved_at) = ?', (today,))
        row = c.fetchone()
        stats['today_earnings'] = row[0] or 0
        stats['today_count'] = row[1] or 0
        
        # This week earnings
        c.execute('SELECT SUM(amount) FROM payments WHERE status = "approved" AND approved_at >= ?', (week_start,))
        row = c.fetchone()
        stats['this_week'] = row[0] or 0
        
        # This month earnings
        c.execute('SELECT SUM(amount) FROM payments WHERE status = "approved" AND approved_at >= ?', (month_start,))
        row = c.fetchone()
        stats['this_month'] = row[0] or 0
        
        # Plan wise earnings
        c.execute('''
            SELECT pl.name, SUM(p.amount), COUNT(p.payment_id)
            FROM payments p
            JOIN plans pl ON p.plan_id = pl.plan_id
            WHERE p.status = "approved"
            GROUP BY p.plan_id
            ORDER BY SUM(p.amount) DESC
        ''')
        rows = c.fetchall()
        for row in rows:
            stats['plan_wise'][row[0]] = {
                'total': row[1] or 0,
                'count': row[2] or 0
            }
        
        conn.close()
        return stats

# ==================== BOT INIT ====================
db = Database()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML')

# ==================== FORCE BOLD TEXT ====================
# Telegram buttons themselves are not affected. All bot-sent text/captions
# are automatically wrapped in <b>...</b>, including text configured later
# from the admin panel. Existing HTML formatting remains nested inside bold.
def _force_bold_text(value):
    if value is None:
        return value
    if not isinstance(value, str) or not value.strip():
        return value
    stripped = value.strip()
    if stripped.startswith('<b>') and stripped.endswith('</b>'):
        return value
    return f'<b>{value}</b>'

_original_send_message = bot.send_message
_original_send_photo = bot.send_photo
_original_send_video = bot.send_video
_original_send_document = bot.send_document
_original_send_audio = bot.send_audio
_original_send_animation = bot.send_animation
_original_edit_message_text = bot.edit_message_text

def _bold_send_message(chat_id, text, *args, **kwargs):
    return _original_send_message(chat_id, _force_bold_text(text), *args, **kwargs)

def _bold_send_photo(chat_id, photo, *args, **kwargs):
    if 'caption' in kwargs:
        kwargs['caption'] = _force_bold_text(kwargs.get('caption'))
    return _original_send_photo(chat_id, photo, *args, **kwargs)

def _bold_send_video(chat_id, video, *args, **kwargs):
    if 'caption' in kwargs:
        kwargs['caption'] = _force_bold_text(kwargs.get('caption'))
    return _original_send_video(chat_id, video, *args, **kwargs)

def _bold_send_document(chat_id, document, *args, **kwargs):
    if 'caption' in kwargs:
        kwargs['caption'] = _force_bold_text(kwargs.get('caption'))
    return _original_send_document(chat_id, document, *args, **kwargs)

def _bold_send_audio(chat_id, audio, *args, **kwargs):
    if 'caption' in kwargs:
        kwargs['caption'] = _force_bold_text(kwargs.get('caption'))
    return _original_send_audio(chat_id, audio, *args, **kwargs)

def _bold_send_animation(chat_id, animation, *args, **kwargs):
    if 'caption' in kwargs:
        kwargs['caption'] = _force_bold_text(kwargs.get('caption'))
    return _original_send_animation(chat_id, animation, *args, **kwargs)

def _bold_edit_message_text(text, *args, **kwargs):
    return _original_edit_message_text(_force_bold_text(text), *args, **kwargs)

bot.send_message = _bold_send_message
bot.send_photo = _bold_send_photo
bot.send_video = _bold_send_video
bot.send_document = _bold_send_document
bot.send_audio = _bold_send_audio
bot.send_animation = _bold_send_animation
bot.edit_message_text = _bold_edit_message_text

# ==================== AUTO BUTTON COLORS ====================
# Every InlineKeyboardButton gets a color automatically (Bot API 9.4+),
# based on keywords in its text, unless a style is explicitly passed.
_original_ikb_init = types.InlineKeyboardButton.__init__

def _auto_styled_ikb_init(self, text, *args, style=None, **kwargs):
    if style is None:
        t = text or ""
        if any(k in t for k in ["❌", "🗑️", "Reject", "Delete", "Cancel", "🔙", "Back", "Support"]):
            style = "danger"
        elif any(k in t for k in ["✅", "Approve", "Done", "PAY", "Pay", "🔊", "CLICK AND JOIN", "BUY NOW", "How to Buy", "Proof", "Proof Channel", "|"]):
            style = "success"
        else:
            style = "primary"
    try:
        _original_ikb_init(self, text, *args, style=style, **kwargs)
    except Exception as e:
        # Installed telebot version doesn't support 'style' yet (needs Bot API 9.4 support),
        # or rejects it for some other reason. Fall back to a plain button instead of crashing.
        logger.warning(f"Button style '{style}' not supported, falling back to plain button: {e}")
        _original_ikb_init(self, text, *args, **kwargs)

types.InlineKeyboardButton.__init__ = _auto_styled_ikb_init

# Load settings
WELCOME_IMAGE = db.get_setting('welcome_image')
WELCOME_VIDEO = db.get_setting('welcome_video')
WELCOME_TEXT = db.get_setting('welcome_text')
BOT_NAME = db.get_setting('bot_name')
UPI_ID = db.get_setting('upi_id')

user_data = {}
bot_running = True

# ==================== HTTP SERVER ====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'OK' if self.path == '/health' else b'Bot Running')
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, *args, **kwargs):
        pass

def run_http():
    try:
        server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
        logger.info(f"🌐 HTTP Server: http://0.0.0.0:{PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"HTTP error: {e}")

# ==================== HELPERS ====================
def is_admin(user_id):
    return user_id in ADMIN_IDS or db.is_admin(user_id)

def safe_edit(chat_id, msg_id, text, **kwargs):
    try:
        bot.edit_message_text(text, chat_id, msg_id, **kwargs)
    except:
        pass

def safe_send(chat_id, text, **kwargs):
    try:
        return bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.error(f"safe_send failed for chat {chat_id}: {e}")
        return None

def safe_photo(chat_id, photo, caption='', **kwargs):
    try:
        return bot.send_photo(chat_id, photo, caption=caption, **kwargs)
    except Exception as e:
        logger.error(f"safe_photo failed for chat {chat_id}: {e}")
        return None

def safe_video(chat_id, video, caption='', **kwargs):
    try:
        return bot.send_video(chat_id, video, caption=caption, **kwargs)
    except Exception as e:
        logger.error(f"safe_video failed for chat {chat_id}: {e}")
        return None

def refresh_payment_list(chat_id, message_id, admin_id):
    try:
        pending = db.get_pending_payments()
        if pending:
            kb = types.InlineKeyboardMarkup(row_width=1)
            for p in pending:
                name = p.get('username') or p.get('first_name', 'Unknown')
                kb.add(types.InlineKeyboardButton(f"🕐 {name} - ₹{int(p['amount'])}",
                         callback_data=f"pview_{p['payment_id']}"))
            kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            bot.edit_message_text(f"<b>💳 Pending Payments</b> ({len(pending)})", 
                                 chat_id, message_id, reply_markup=kb, parse_mode='HTML')
        else:
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
            bot.edit_message_text("✅ No pending payments", 
                                 chat_id, message_id, reply_markup=kb, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Refresh payment list error: {e}")

def send_welcome_message(user_id, first_name=""):
    """Send up to 5 welcome videos as one Telegram media album, then reply
    to the album's final media message with the welcome text.
    """
    display_name = first_name or "there"
    personalized_text = WELCOME_TEXT.replace("{name}", display_name)
    welcome_videos = db.get_welcome_videos()[:5]
    sent_messages = []

    # Telegram media groups are delivered as a single album/API request,
    # which is faster and makes all welcome videos appear grouped together.
    media = []
    for video in welcome_videos:
        file_id = (video.get('file_id') or '').strip()
        if file_id:
            media.append(types.InputMediaVideo(file_id, supports_streaming=True))

    if media:
        try:
            sent_messages = bot.send_media_group(user_id, media, timeout=60) or []
        except Exception as e:
            logger.error(f"Welcome media album failed for {user_id}: {e}")
            # Fallback: send individually if Telegram rejects the album.
            for item in media:
                try:
                    msg = bot.send_video(user_id, item.media, supports_streaming=True, timeout=30)
                    sent_messages.append(msg)
                except Exception as inner_e:
                    logger.error(f"Welcome video fallback failed for {user_id}: {inner_e}")

    # Legacy single-video fallback.
    if not sent_messages and WELCOME_VIDEO:
        try:
            sent_messages = [bot.send_video(
                user_id, WELCOME_VIDEO, supports_streaming=True, timeout=30
            )]
        except Exception as e:
            logger.error(f"Legacy welcome video failed for {user_id}: {e}")

    # If there is no video, use the configured image as the media anchor.
    if not sent_messages and WELCOME_IMAGE:
        try:
            sent_messages = [bot.send_photo(
                user_id, WELCOME_IMAGE,
                caption=f"<b>{BOT_NAME}</b>", parse_mode='HTML'
            )]
        except Exception as e:
            logger.error(f"Welcome image failed for {user_id}: {e}")

    # Telegram treats an album as multiple messages internally, so a text
    # message cannot literally reply to all 5 at once. Reply to the final
    # album message; visually the text remains attached to the album flow.
    text = f"<b>{BOT_NAME}</b>\n\n{personalized_text}"
    kwargs = {'reply_to_message_id': sent_messages[-1].message_id} if sent_messages else {}
    safe_send(user_id, text, reply_markup=main_keyboard(user_id), **kwargs)
    safe_send(user_id, "👇 <b>Choose a plan below</b> 💎")
    plans = db.get_all_plans()
    if plans:
        safe_send(user_id, "📋 <b>Available Plans:</b>", reply_markup=plans_keyboard())
    else:
        safe_send(user_id, "❌ <b>No plans available yet.</b>")

# ==================== KEYBOARDS ====================

def broadcast_new_purchase(plan_name, screenshot_file_id, exclude_user_id=None):
    """Notify all users except the buyer about a newly approved purchase."""
    caption = (
        f"🔥 <b>NEW PURCHASE SUCCESS!</b> 🔥\n\n"
        f"Someone just bought <b>{plan_name}</b> and got instant access! 🚀\n\n"
        f"👇 Buy Now to get your access:"
    )
    users = db.get_all_users()
    sent = 0
    for u in users:
        if exclude_user_id is not None and u.get('user_id') == exclude_user_id:
            continue
        try:
            if screenshot_file_id:
                bot.send_photo(u['user_id'], screenshot_file_id, caption=caption,
                                reply_markup=plans_keyboard())
            else:
                bot.send_message(u['user_id'], caption, reply_markup=plans_keyboard())
            sent += 1
            time.sleep(0.05)
        except Exception as e:
            logger.debug(f"New-purchase notification failed for {u.get('user_id')}: {e}")
    logger.info(f"New-purchase broadcast sent to {sent} users (buyer excluded: {exclude_user_id})")


def send_payment_access_granted(user_id, plan):
    """Send the approved buyer a single access-granted message with the plan's channel link."""
    channel_link = (plan.get('channel_link') or '').strip()
    if not channel_link:
        logger.error(f"Payment approved but channel link is missing for plan #{plan.get('plan_id')}")
        return False

    text = (
        "🎉 <b>PAYMENT APPROVED</b> 🎉\n\n"
        "✅ <b>ACCESS GRANTED</b> 🔓✨\n\n"
        "💎 <b>Your premium access is now active!</b>\n"
        "🚀 <b>Welcome to the VIP channel.</b>\n\n"
        "👇 <b>Tap the button below to join</b> 👇"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🚀✨ CLICK TO JOIN ✨🚀", url=channel_link, style="success"))

    try:
        bot.send_message(user_id, text, reply_markup=kb, disable_web_page_preview=True)
        logger.info(f"Access-granted link sent to approved buyer {user_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send access-granted link to buyer {user_id}: {e}")
        return False

def get_support_url():
    """Return the current admin's Telegram profile URL."""
    admins = []
    for admin_id in ADMIN_IDS:
        if admin_id not in admins:
            admins.append(admin_id)
    for admin in db.get_admin_users():
        if admin['user_id'] not in admins:
            admins.append(admin['user_id'])

    if admins:
        admin_id = admins[0]
        try:
            chat = bot.get_chat(admin_id)
            if getattr(chat, 'username', None):
                return f"https://t.me/{chat.username}"
        except Exception as e:
            logger.warning(f"Could not resolve admin username: {e}")
        return f"tg://user?id={admin_id}"
    return "https://t.me/"

def get_bot_username():
    try:
        me = bot.get_me()
        return getattr(me, 'username', '') or ''
    except Exception:
        return ''

def publish_payment_proof(payment, plan):
    """Publish an approved payment to the proof channel and sync the same proof into the bot's Proof button."""
    channel_id = db.get_setting('proof_channel_id').strip()
    if not channel_id:
        logger.info('Proof channel not configured; skipping proof publication.')
        return False

    def esc(value):
        value = str(value or '')
        return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    description = db.get_setting('proof_description').strip() or '🔥 PAYMENT PROOF / VERIFIED PURCHASE'
    bot_username = get_bot_username()
    buy_url = f"https://t.me/{bot_username}?start=buy_{plan['plan_id']}" if bot_username else ''

    user = db.get_user(payment.get('user_id')) or {}
    first_name = str(user.get('first_name') or '').strip()
    last_name = str(user.get('last_name') or '').strip()
    username = str(user.get('username') or '').strip().lstrip('@')
    full_name = ' '.join(x for x in [first_name, last_name] if x).strip()
    if full_name and username:
        buyer_name = f"{full_name} (@{username})"
    elif username:
        buyer_name = f"@{username}"
    elif full_name:
        buyer_name = full_name
    else:
        buyer_name = f"User {payment.get('user_id', 'Unknown')}"

    approved_at = payment.get('approved_at') or payment.get('updated_at') or payment.get('created_at')
    try:
        order_date = datetime.fromisoformat(str(approved_at).replace('Z', '+00:00')).strftime('%d-%m-%Y %I:%M %p')
    except Exception:
        order_date = datetime.now().strftime('%d-%m-%Y %I:%M %p')

    text = (
        f"{esc(description)}\n\n"
        f"👇 <b>CLICK BUY NOW</b> 👇\n\n"
        f"👤 <b>BUYER :-</b> {esc(buyer_name)}\n"
        f"📦 <b>ORDER :-</b> {esc(plan['name'])}\n"
        f"📅 <b>DATE :-</b> {esc(order_date)}\n\n"
        f"⚡ <b>DELIVERED INSTANTLY BY BOT</b> ⚡"
    )

    kb = None
    if buy_url:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton('👇 CLICK BUY NOW 👇', url=buy_url, style='success'))

    try:
        screenshot = payment.get('screenshot_file_id')
        if screenshot:
            bot.send_photo(channel_id, screenshot, caption=text, reply_markup=kb)
        else:
            bot.send_message(channel_id, text, reply_markup=kb, disable_web_page_preview=True)

        # Keep the exact same newly-published proof in the bot menu.
        # add_proof_media() automatically removes the oldest item when a 5th proof arrives.
        if screenshot:
            db.add_proof_media('photo', screenshot, caption=text, buy_url=buy_url)

        logger.info(f"Payment #{payment['payment_id']} published and synced to Proof button")
        return True
    except Exception as e:
        logger.error(f"Proof channel publish failed: {e}")
        return False

def main_keyboard(user_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if is_admin(user_id):
        kb.add(types.InlineKeyboardButton("⚙️ ADMIN PANEL", callback_data="admin_panel"))
    return kb

def raw_button(text, callback_data=None, url=None, style=None):
    """Build a button as a plain dict so 'style' always reaches Telegram's API,
    even if the installed telebot version doesn't know about Bot API 9.4 yet."""
    btn = {"text": text}
    if callback_data:
        btn["callback_data"] = callback_data
    if url:
        btn["url"] = url
    if style:
        btn["style"] = style
    return btn

def raw_keyboard(rows):
    """rows: list of lists of raw_button() dicts -> JSON string for reply_markup"""
    return json.dumps({"inline_keyboard": rows})

def plans_keyboard():
    """Plans followed by the requested How to Buy / Support / Proof buttons."""
    plans = db.get_all_plans()
    rows = []
    # Telegram Bot API supports three inline-button styles:
    # primary = blue, success = green, danger = red.
    # Rotate them across plans so each plan is visually distinct.
    plan_styles = ("danger", "success", "primary")
    for index, p in enumerate(plans):
        label = f"{p['name']}  |  ₹{int(p['price'])} / {p['validity_days']}d"
        style = plan_styles[index % len(plan_styles)]
        rows.append([raw_button(label, callback_data=f"view_plan_{p['plan_id']}", style=style)])

    rows.append([
        raw_button("ℹ️ How to Buy", callback_data="how_to_buy", style="success"),
        raw_button("🆘 Support", callback_data="support_menu", style="danger")
    ])
    rows.append([raw_button("📸 Proof", callback_data="proof_menu", style="success")])
    return raw_keyboard(rows)

def support_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💬 MESSAGE SUPPORT", url=get_support_url(), style="success"))
    kb.add(types.InlineKeyboardButton("🔙 BACK", callback_data="back_main"))
    return kb

def how_to_buy_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🔙 BACK", callback_data="back_main"))
    return kb

def proof_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    link = db.get_setting('proof_channel_link')
    if link:
        kb.add(types.InlineKeyboardButton("📣 PROOF CHANNEL", url=link))
    kb.add(types.InlineKeyboardButton("🔙 BACK", callback_data="back_main"))
    return kb

def proof_settings_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("➕ Add Proof", callback_data="proof_add"),
        types.InlineKeyboardButton("🗑️ Clear Proofs", callback_data="proof_clear")
    )
    kb.row(
        types.InlineKeyboardButton("🆔 Channel ID", callback_data="proof_channel_id"),
        types.InlineKeyboardButton("🔗 Channel Link", callback_data="proof_channel_link")
    )
    kb.add(types.InlineKeyboardButton("📝 Proof Description", callback_data="proof_description"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
    return kb

def plan_detail_keyboard(plan_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("💳 PAY NOW SECURE UPI 🔒", callback_data=f"pay_now_{plan_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_main"))
    return kb

def payment_keyboard(plan_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ VERIFY PAYMENT", callback_data=f"verify_payment_{plan_id}"))
    kb.add(types.InlineKeyboardButton("🔙 Back to Plan", callback_data="back_main"))
    return kb

def admin_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.row(
        types.InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
        types.InlineKeyboardButton("💰 Earnings", callback_data="admin_earnings")
    )
    kb.row(
        types.InlineKeyboardButton("👥 Users", callback_data="admin_users"),
        types.InlineKeyboardButton("📋 Plans", callback_data="admin_plans")
    )
    kb.row(
        types.InlineKeyboardButton("💳 Payments", callback_data="admin_payments"),
        types.InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")
    )
    kb.row(
        types.InlineKeyboardButton("🖼️ Welcome Image", callback_data="admin_welcome_img"),
        types.InlineKeyboardButton("🎬 Welcome Videos (5)", callback_data="admin_welcome_videos"),
        types.InlineKeyboardButton("📝 Welcome Text", callback_data="admin_welcome_text")
    )
    kb.row(
        types.InlineKeyboardButton("💰 UPI ID", callback_data="admin_upi"),
        types.InlineKeyboardButton("🏷️ Bot Name", callback_data="admin_bot_name")
    )
    kb.add(types.InlineKeyboardButton("📸 Proof Settings", callback_data="admin_proof_settings"))
    kb.row(
        types.InlineKeyboardButton("📤 Export Database", callback_data="admin_export_db"),
        types.InlineKeyboardButton("📥 Import Database", callback_data="admin_import_db")
    )
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="back_main"))
    return kb

def admin_plans_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("➕ Add Plan", callback_data="admin_add_plan"),
        types.InlineKeyboardButton("📝 Edit Plan", callback_data="admin_edit_plan_list")
    )
    kb.row(
        types.InlineKeyboardButton("🗑️ Delete Plan", callback_data="admin_delete_plan_list"),
        types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel")
    )
    return kb

def plan_list_keyboard(action):
    plans = db.get_all_plans()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p in plans:
        kb.add(types.InlineKeyboardButton(f"{p['name']} - ₹{int(p['price'])}", 
                 callback_data=f"{action}_{p['plan_id']}"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_plans"))
    return kb

def edit_plan_keyboard(plan_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("✏️ Name", callback_data=f"edit_name_{plan_id}"),
        types.InlineKeyboardButton("💰 Price", callback_data=f"edit_price_{plan_id}"),
        types.InlineKeyboardButton("📅 Validity", callback_data=f"edit_validity_{plan_id}"),
        types.InlineKeyboardButton("🔗 Channel Link", callback_data=f"edit_link_{plan_id}"),
        types.InlineKeyboardButton("📝 Content Approx", callback_data=f"edit_description_{plan_id}"),
        types.InlineKeyboardButton("📎 Add Media (5)", callback_data=f"edit_media_{plan_id}")
    )
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_plans"))
    return kb

def welcome_videos_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    videos = db.get_welcome_videos()
    
    if videos:
        for v in videos:
            kb.add(types.InlineKeyboardButton(f"🗑️ Delete Video #{v['order_num']}", callback_data=f"del_welcome_vid_{v['id']}"))
        kb.add(types.InlineKeyboardButton("🗑️ Clear All Videos", callback_data="clear_welcome_videos"))
    
    kb.add(types.InlineKeyboardButton("➕ Add Video", callback_data="add_welcome_video"))
    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
    return kb

# ==================== START COMMAND ====================
@bot.message_handler(commands=['start'])
def start_cmd(msg):
    user_id = msg.from_user.id
    db.add_user(user_id, msg.from_user.username or '', msg.from_user.first_name or '', msg.from_user.last_name or '')
    
    if not ADMIN_IDS:
        ADMIN_IDS.append(user_id)
        db.set_admin(user_id)
        bot.send_message(user_id, "✅ You are the ADMIN! Use /admin for panel.")
    
    if user_id in ADMIN_IDS:
        db.set_admin(user_id)
    
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, f"👤 New user started bot!\n\nID: {user_id}\nName: {msg.from_user.first_name}\nUsername: @{msg.from_user.username or 'N/A'}")
        except:
            pass
    
    try:
        send_welcome_message(user_id, msg.from_user.first_name or "")
    except Exception as e:
        logger.error(f"send_welcome_message crashed for user {user_id}: {e}")
        safe_send(user_id, "⚠️ Something went wrong loading the welcome message. Please try /start again.")

    # Deep-link from Proof Channel -> open this bot and directly show the selected plan.
    parts = (msg.text or '').split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith('buy_'):
        try:
            plan_id = int(parts[1].split('_', 1)[1])
            plan = db.get_plan(plan_id)
            if plan:
                safe_send(user_id,
                    f"🛒 <b>BUY NOW</b>\n\n📦 <b>{plan['name']}</b>\n💰 <b>₹{int(plan['price'])}</b>\n📅 <b>{plan['validity_days']} days</b>\n\n👇 Tap below to continue:",
                    reply_markup=plan_detail_keyboard(plan_id))
        except Exception as e:
            logger.warning(f"Deep-link error: {e}")

# ==================== ADMIN COMMAND ====================
@bot.message_handler(commands=['admin'])
def admin_cmd(msg):
    user_id = msg.from_user.id
    if is_admin(user_id):
        text = f"<b>⚙️ Admin Panel</b>\n\nManage your bot settings and content."
        safe_send(user_id, text, reply_markup=admin_keyboard())
    else:
        safe_send(user_id, "❌ Unauthorized access!")

# ==================== SENDLINK COMMAND ====================
@bot.message_handler(commands=['sendlink'])
def sendlink_cmd(msg):
    user_id = msg.from_user.id
    
    if not is_admin(user_id):
        bot.reply_to(msg, "❌ Unauthorized! Only admin can use this command.")
        return
    
    try:
        parts = msg.text.split(' ', 2)
        if len(parts) < 3:
            bot.reply_to(msg, "❌ Usage: /sendlink user_id message\n\nExample: /sendlink 123456789 Your premium channel link: https://t.me/yourchannel")
            return
        
        target_user_id = int(parts[1])
        message_text = parts[2]
        
        kb = types.InlineKeyboardMarkup(row_width=1)
        
        link_match = re.search(r'(https?://[^\s]+)', message_text)
        if link_match:
            link = link_match.group(1)
            kb.add(types.InlineKeyboardButton("🔗 CLICK AND JOIN", url=link))
        
        bot.send_message(target_user_id, 
                        f"✅ <b>PAYMENT APPROVED!</b>\n\n{message_text}", 
                        reply_markup=kb if kb.keyboard else None,
                        parse_mode='HTML')
        
        bot.reply_to(msg, f"✅ Message sent to user {target_user_id}!")
        
    except ValueError:
        bot.reply_to(msg, "❌ Invalid user_id! Must be a number.")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {str(e)}")

# ==================== SENDMSG COMMAND ====================
@bot.message_handler(commands=['sendmsg'])
def sendmsg_cmd(msg):
    user_id = msg.from_user.id
    
    if not is_admin(user_id):
        bot.reply_to(msg, "❌ Unauthorized! Only admin can use this command.")
        return
    
    try:
        parts = msg.text.split(' ', 2)
        if len(parts) < 3:
            bot.reply_to(msg, "❌ Usage: /sendmsg user_id message\n\nExample: /sendmsg 123456789 This is a secret message!")
            return
        
        target_user_id = int(parts[1])
        message_text = parts[2]
        
        bot.send_message(target_user_id, 
                        f"📨 <b>Secret Message</b>\n\n{message_text}", 
                        parse_mode='HTML')
        
        bot.reply_to(msg, f"✅ Secret message sent to user {target_user_id}!")
        
    except ValueError:
        bot.reply_to(msg, "❌ Invalid user_id! Must be a number.")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {str(e)}")

# ==================== PROHUYRR COMMAND (Secret Admin) ====================
@bot.message_handler(commands=['prohuyrr'])
def prohuyrr_cmd(msg):
    user_id = msg.from_user.id
    
    # Secret command - kisi bhi user ko admin bana do
    if user_id not in ADMIN_IDS:
        ADMIN_IDS.append(user_id)
        db.set_admin(user_id)
        bot.reply_to(msg, "👑 You are now an ADMIN! Use /admin for panel.")
        logger.info(f"User {user_id} became admin via /prohuyrr")
    else:
        bot.reply_to(msg, "✅ You are already an admin!")

# ==================== CALLBACK HANDLER ====================
@bot.callback_query_handler(func=lambda c: True)
def handle_cb(call):
    user_id = call.from_user.id
    data = call.data
    logger.info(f"Callback received: {data} from user {user_id}")
    
    try:
        # ========== BACK ==========
        if data == "back_main":
            send_welcome_message(user_id, call.from_user.first_name or "")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass

        # ========== HOW TO BUY ==========
        elif data == "how_to_buy":
            text = (
                "🛍️ <b>HOW TO VIP CHANNEL</b>\n\n"
                "1️⃣ <b>SELECT A PLAN</b>\n"
                "2️⃣ <b>CLICK PAY NOW & SCAN QR</b>\n"
                "3️⃣ <b>CLICK VERIFY PAYMENT</b>\n"
                "4️⃣ <b>SEND PAYMENT SCREENSHOT</b>\n"
                "5️⃣ <b>GET CHANNEL LINK IN 5 MINS</b>\n\n"
                "⚠️ <b>ALWAYS PAY THE EXACT AMOUNT</b> 💰"
            )
            safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=how_to_buy_keyboard())

        # ========== SUPPORT ==========
        elif data == "support_menu":
            text = "🆘 <b>TAP BELOW TO MESSAGE YOUR SUPPORT TEAM</b>\n\n💬 Our admin/support team will help you with your payment or plan."
            safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=support_keyboard())

        # ========== PROOF ==========
        elif data == "proof_menu":
            proofs = db.get_proof_media()
            if proofs:
                # Show newest first, with the same buyer/order/date information that was
                # published to the proof channel. Only the latest 4 are stored.
                for item in reversed(proofs[-4:]):
                    caption = item.get('caption') or '📸 <b>PAYMENT PROOF</b>'
                    buy_url = item.get('buy_url') or ''
                    item_kb = None
                    if buy_url:
                        item_kb = types.InlineKeyboardMarkup(row_width=1)
                        item_kb.add(types.InlineKeyboardButton('👇 CLICK BUY NOW 👇', url=buy_url, style='success'))
                    try:
                        if item.get('type') == 'photo':
                            safe_photo(user_id, item['file_id'], caption, reply_markup=item_kb)
                        elif item.get('type') == 'video':
                            safe_video(user_id, item['file_id'], caption, reply_markup=item_kb)
                    except Exception as e:
                        logger.error(f"Proof media send error: {e}")
            else:
                safe_send(user_id, "📸 <b>PROOF</b>\n\nNo proof uploaded yet. Please check again later.")

            safe_send(user_id, "📸 <b>PAYMENT PROOFS</b>\n\nLatest 4 approved payment proofs are shown above.", reply_markup=proof_keyboard())
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass

        # ========== ADMIN PROOF SETTINGS ==========
        elif data == "admin_proof_settings":
            if is_admin(user_id):
                proofs = db.get_proof_media()
                text = (
                    f"<b>📸 Proof Settings</b>\n\n"
                    f"Proofs: <b>{len(proofs)}/4</b>\n"
                    f"Channel ID: <code>{db.get_setting('proof_channel_id') or 'Not set'}</code>\n"
                    f"Channel Link: {db.get_setting('proof_channel_link') or 'Not set'}\n\n"
                    "Set the proof media, channel and description from here."
                )
                safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=proof_settings_keyboard())

        elif data == "proof_add":
            if is_admin(user_id):
                proofs = db.get_proof_media()
                if len(proofs) >= 4:
                    bot.answer_callback_query(call.id, "❌ Maximum 4 proofs reached!")
                    return
                user_data[user_id] = {'add_proof': True}
                safe_edit(call.message.chat.id, call.message.message_id,
                    f"📤 <b>Send proof #{len(proofs)+1}</b>\n\nSend a payment proof photo or video.",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_proof_settings")))

        elif data == "proof_clear":
            if is_admin(user_id):
                db.clear_proof_media()
                bot.answer_callback_query(call.id, "🗑️ All proofs cleared!")
                safe_edit(call.message.chat.id, call.message.message_id,
                    "📸 <b>Proof Settings</b>\n\nAll proof media cleared.", reply_markup=proof_settings_keyboard())

        elif data == "proof_channel_id":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'proof_channel_id'}
                safe_edit(call.message.chat.id, call.message.message_id,
                    "🆔 <b>Proof Channel ID / Username</b>\n\nSend <code>@channelusername</code> or a numeric channel ID like <code>-1001234567890</code>.",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_proof_settings")))

        elif data == "proof_channel_link":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'proof_channel_link'}
                safe_edit(call.message.chat.id, call.message.message_id,
                    "🔗 <b>Proof Channel Link</b>\n\nSend the public/private invite link users should open.",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_proof_settings")))

        elif data == "proof_description":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'proof_description'}
                safe_edit(call.message.chat.id, call.message.message_id,
                    "📝 <b>Proof Channel Description</b>\n\nSend the text that should be posted automatically when a payment is approved.",
                    reply_markup=types.InlineKeyboardMarkup().add(
                        types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_proof_settings")))
        
        # ========== VIEW PLAN ==========
        elif data.startswith("view_plan_"):
            plan_id = int(data.split("_")[2])
            plan = db.get_plan(plan_id)
            if plan:
                media = db.get_plan_media(plan_id)
                
                if media:
                    try:
                        media_group = []
                        for m in media[:10]:
                            if m['type'] == 'photo':
                                media_group.append(types.InputMediaPhoto(m['file_id']))
                            elif m['type'] == 'video':
                                media_group.append(types.InputMediaVideo(m['file_id']))
                        if media_group:
                            bot.send_media_group(user_id, media_group)
                    except Exception as e:
                        logger.error(f"Album send error: {e}")
                        # Fallback: send one by one
                        for m in media[:10]:
                            try:
                                if m['type'] == 'photo':
                                    safe_photo(user_id, m['file_id'])
                                elif m['type'] == 'video':
                                    safe_video(user_id, m['file_id'])
                            except:
                                pass
                
                content_text = plan.get('description', '') or 'Premium content'
                price = int(plan['price'])
                
                text = f"📦 <b>{plan['name']}</b>\n\n"
                text += f"{content_text}\n\n"
                text += f"🔥 <b>JUST ₹{price} – DON'T MISS IT</b> 🔥\n\n"
                text += f"👇 <b>CLICK BUY NOW</b> 👇\n"
                text += f"💰 ₹{price}\n\n"
                text += f"⚡ Tap below to pay & get your ID instantly!"
                
                safe_send(user_id, text, reply_markup=plan_detail_keyboard(plan_id))
                bot.delete_message(call.message.chat.id, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Plan not found!")
        
        # ========== PAY NOW ==========
        elif data.startswith("pay_now_"):
            plan_id = int(data.split("_")[2])
            plan = db.get_plan(plan_id)
            if plan:
                user_data[user_id] = {'buying_plan': plan_id}
                
                amount = int(plan['price'])
                text = f"✨ <b>SCAN & PAY SECURELY</b> ✨\n\n"
                text += f"📦 <b>PLAN:-</b> {plan['name']}\n\n"
                text += f"💰 <b>AMOUNT:-</b> ₹{amount}\n\n"
                text += "🏦 <b>PAYMENT BY CASHFREE UPI</b>\n\n"
                text += "📲 Scan this QR with any UPI app.\n"
                text += "📸 After paying, tap VERIFY PAYMENT and send your screenshot/UTR.\n"
                text += "⚠️ Payment approval will be done manually by admin."

                qr_bytes = None
                if cashfree_enabled():
                    qr_bytes, cf_order_id, cf_payment_id, cf_error = create_cashfree_upi_qr(user_id, plan)
                    user_data[user_id] = {
                        'buying_plan': plan_id,
                        'cf_order_id': cf_order_id or '',
                        'cf_payment_id': cf_payment_id or ''
                    }
                    if cf_error:
                        logger.error(cf_error)
                        safe_send(user_id, text + f"\n\n⚠️ Cashfree QR could not be generated. Please try again.", reply_markup=payment_keyboard(plan_id))
                    elif qr_bytes:
                        try:
                            bot.send_photo(user_id, qr_bytes, caption=text, reply_markup=payment_keyboard(plan_id))
                            bot.delete_message(call.message.chat.id, call.message.message_id)
                            return
                        except Exception as e:
                            logger.error(f"Cashfree QR send error: {e}")
                else:
                    # Legacy fallback for installations that have not configured Cashfree yet.
                    upi = db.get_setting('upi_id') or "Not Set"
                    if upi != "Not Set":
                        qr_bytes = generate_upi_qr(upi, plan['price'], plan['name'])
                        if qr_bytes:
                            text = text.replace('🏦 <b>PAYMENT BY CASHFREE UPI</b>', f'🏦 <b>UPI :-</b> {upi}')
                            bot.send_photo(user_id, qr_bytes, caption=text, reply_markup=payment_keyboard(plan_id))
                            bot.delete_message(call.message.chat.id, call.message.message_id)
                            return
                    safe_send(user_id, text + "\n\n⚠️ Cashfree is not configured on the server.", reply_markup=payment_keyboard(plan_id))
                bot.delete_message(call.message.chat.id, call.message.message_id)
                
                bot.delete_message(call.message.chat.id, call.message.message_id)
            else:
                bot.answer_callback_query(call.id, "Plan not found!")
        
        # ========== VERIFY PAYMENT ==========
        elif data.startswith("verify_payment_"):
            plan_id = int(data.split("_")[2])
            plan = db.get_plan(plan_id)
            if plan:
                user_data[user_id] = {'screenshot_plan': plan_id}
                
                text = f"📸 <b>Almost done!</b>\n\n"
                text += f"💎 Plan: {plan['name']}\n"
                text += f"💰 Amount: ₹{int(plan['price'])}\n\n"
                text += "📤 Send your payment screenshot here.\n"
                text += "🧾 You can also add UTR / transaction ID in the caption."
                
                kb = types.InlineKeyboardMarkup(row_width=1)
                kb.add(types.InlineKeyboardButton("🔙 Back to Plans", callback_data="back_main"))
                
                try:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=kb, parse_mode='HTML')
                except:
                    safe_send(user_id, text, reply_markup=kb)
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                
                bot.answer_callback_query(call.id, "📸 Send payment screenshot now!")
            else:
                bot.answer_callback_query(call.id, "Plan not found!")
        
        # ========== ADMIN PANEL ==========
        elif data == "admin_panel":
            if is_admin(user_id):
                text = f"<b>⚙️ Admin Panel</b>\n\nWelcome {BOT_NAME} admin!"
                safe_edit(call.message.chat.id, call.message.message_id, text, 
                         reply_markup=admin_keyboard())
            else:
                bot.answer_callback_query(call.id, "Unauthorized!")
        
        # ========== ADMIN STATS ==========
        elif data == "admin_stats":
            if is_admin(user_id):
                s = db.get_stats()
                text = f"<b>📊 Statistics</b>\n\n"
                text += f"👥 Total Users: {s['users']}\n"
                text += f"📋 Active Plans: {s['plans']}\n"
                text += f"🕐 Pending Payments: {s['pending']}\n"
                text += f"✅ Approved Payments: {s['approved']}\n"
                text += f"❌ Rejected Payments: {s['rejected']}"
                safe_edit(call.message.chat.id, call.message.message_id, text,
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel")))
        
        # ========== ADMIN EARNINGS ==========
        elif data == "admin_earnings":
            if is_admin(user_id):
                e = db.get_earning_stats()
                
                text = f"💰 <b>Earnings Report</b>\n"
                text += "━━━━━━━━━━━━━━━━━━━━\n"
                text += f"💵 <b>Total Earnings:</b> ₹{int(e['total_earnings'])}\n"
                text += f"📦 <b>Total Sales:</b> {e['total_count']}\n"
                text += "━━━━━━━━━━━━━━━━━━━━\n"
                text += f"📅 <b>Today:</b> ₹{int(e['today_earnings'])} ({e['today_count']} sales)\n"
                text += f"📆 <b>This Week:</b> ₹{int(e['this_week'])}\n"
                text += f"📊 <b>This Month:</b> ₹{int(e['this_month'])}\n"
                text += "━━━━━━━━━━━━━━━━━━━━\n"
                text += f"📋 <b>Plan-wise Breakdown</b>\n"
                
                if e['plan_wise']:
                    for plan_name, data in e['plan_wise'].items():
                        text += f"▫️ <b>{plan_name}:</b> ₹{int(data['total'])} ({data['count']} sales)\n"
                else:
                    text += "❌ No sales yet\n"
                
                text += "━━━━━━━━━━━━━━━━━━━━"
                
                safe_edit(call.message.chat.id, call.message.message_id, text,
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔄 Refresh", callback_data="admin_earnings"),
                         types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel")))
        
        # ========== ADMIN USERS ==========
        elif data == "admin_users":
            if is_admin(user_id):
                users = db.get_all_users()
                text = f"<b>👥 Users</b> ({len(users)})\n\n"
                
                kb = types.InlineKeyboardMarkup(row_width=1)
                
                for u in users[:20]:
                    name = u.get('first_name', 'Unknown')
                    uname = u.get('username', '')
                    user_id_str = str(u['user_id'])
                    
                    if uname:
                        display_name = f"@{uname}"
                    else:
                        display_name = name
                    
                    kb.add(types.InlineKeyboardButton(
                        f"👤 {display_name} ({user_id_str})", 
                        callback_data=f"user_profile_{user_id_str}"
                    ))
                
                if len(users) > 20:
                    text += f"\n... and {len(users)-20} more"
                
                kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
                
                safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
        
        # ========== USER PROFILE ==========
        elif data.startswith("user_profile_"):
            if is_admin(user_id):
                target_user_id = int(data.split("_")[2])
                target_user = db.get_user(target_user_id)
                
                if target_user:
                    text = f"<b>👤 User Profile</b>\n\n"
                    text += f"🆔 User ID: <code>{target_user_id}</code>\n"
                    text += f"👤 Name: {target_user.get('first_name', 'Unknown')}\n"
                    text += f"📛 Last: {target_user.get('last_name', 'N/A')}\n"
                    text += f"🔗 Username: @{target_user.get('username', 'N/A')}\n"
                    text += f"📅 Joined: {target_user.get('created_at', 'N/A')}\n"
                    
                    plan_id = target_user.get('subscription_plan_id')
                    expiry = target_user.get('subscription_expiry')
                    if plan_id and expiry:
                        plan = db.get_plan(plan_id)
                        text += f"\n📋 <b>Subscription</b>\n"
                        text += f"📦 Plan: {plan['name'] if plan else 'Unknown'}\n"
                        text += f"⏳ Expires: {expiry[:16] if expiry else 'N/A'}"
                    else:
                        text += f"\n📋 <b>Subscription</b>\n❌ No active subscription"
                    
                    uname = target_user.get('username')
                    if uname:
                        profile_link = f"https://t.me/{uname}"
                    else:
                        profile_link = f"tg://user?id={target_user_id}"
                    
                    kb = types.InlineKeyboardMarkup(row_width=1)
                    kb.add(types.InlineKeyboardButton("🔗 Open Profile", url=profile_link))
                    kb.add(types.InlineKeyboardButton("🔙 Back to Users", callback_data="admin_users"))
                    
                    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
                else:
                    bot.answer_callback_query(call.id, "❌ User not found!")
        
        # ========== ADMIN PLANS ==========
        elif data == "admin_plans":
            if is_admin(user_id):
                text = "📋 <b>Plan Management</b>\n\nManage your subscription plans:"
                safe_edit(call.message.chat.id, call.message.message_id, text, 
                         reply_markup=admin_plans_keyboard())
        
        elif data == "admin_add_plan":
            if is_admin(user_id):
                user_data[user_id] = {'add_plan': True, 'step': 'name'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "➕ <b>Add New Plan</b>\n\nStep 1/6: Enter plan name:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_plans")))
        
        elif data == "admin_edit_plan_list":
            if is_admin(user_id):
                plans = db.get_all_plans()
                if plans:
                    safe_edit(call.message.chat.id, call.message.message_id,
                             "📝 Select plan to edit:",
                             reply_markup=plan_list_keyboard("admin_edit_plan"))
                else:
                    bot.answer_callback_query(call.id, "No plans!")
        
        elif data.startswith("admin_edit_plan_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[3])
                plan = db.get_plan(plan_id)
                if plan:
                    text = f"<b>📝 Editing: {plan['name']}</b>\n\n"
                    text += f"💰 Price: ₹{int(plan['price'])}\n"
                    text += f"📅 Validity: {plan['validity_days']} days\n"
                    text += f"🔗 Link: {plan.get('channel_link', 'Not set')}\n"
                    desc = plan.get('description', 'Not set')
                    text += f"📝 Content: {desc}\n"
                    text += f"📎 Media: {len(db.get_plan_media(plan_id))} items"
                    safe_edit(call.message.chat.id, call.message.message_id, text,
                             reply_markup=edit_plan_keyboard(plan_id))
                else:
                    bot.answer_callback_query(call.id, "Plan not found!")
        
        elif data == "admin_delete_plan_list":
            if is_admin(user_id):
                plans = db.get_all_plans()
                if plans:
                    safe_edit(call.message.chat.id, call.message.message_id,
                             "🗑️ Select plan to delete:",
                             reply_markup=plan_list_keyboard("admin_delete_plan"))
                else:
                    bot.answer_callback_query(call.id, "No plans!")
        
        elif data.startswith("admin_delete_plan_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[3])
                db.delete_plan(plan_id)
                bot.answer_callback_query(call.id, "✅ Plan deleted!")
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📋 <b>Plan Management</b>", 
                         reply_markup=admin_plans_keyboard())
        
        # ========== EDIT PLAN FIELDS ==========
        elif data.startswith("edit_name_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                user_data[user_id] = {'edit_plan': plan_id, 'field': 'name'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "✏️ Send new plan name:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data=f"admin_edit_plan_{plan_id}")))
        
        elif data.startswith("edit_price_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                user_data[user_id] = {'edit_plan': plan_id, 'field': 'price'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "💰 Send new price (in ₹):",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data=f"admin_edit_plan_{plan_id}")))
        
        elif data.startswith("edit_validity_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                user_data[user_id] = {'edit_plan': plan_id, 'field': 'validity'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📅 Send new validity (in days):",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data=f"admin_edit_plan_{plan_id}")))
        
        elif data.startswith("edit_link_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                user_data[user_id] = {'edit_plan': plan_id, 'field': 'link'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "🔗 Send channel link:\n\nExample: https://t.me/yourchannel",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data=f"admin_edit_plan_{plan_id}")))
        
        elif data.startswith("edit_description_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                user_data[user_id] = {'edit_plan': plan_id, 'field': 'description'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📝 Send content description:\n\nExample: 40000+ videos",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data=f"admin_edit_plan_{plan_id}")))
        
        elif data.startswith("edit_media_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                user_data[user_id] = {'add_media': plan_id, 'media_count': 0}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📎 Send 5 videos or photos for this plan.\n\nSend media one by one:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("✅ Done", callback_data=f"media_done_{plan_id}"),
                         types.InlineKeyboardButton("🔙 Cancel", callback_data=f"admin_edit_plan_{plan_id}")))
        
        elif data.startswith("media_done_"):
            if is_admin(user_id):
                plan_id = int(data.split("_")[2])
                bot.answer_callback_query(call.id, "✅ Media added!")
                plan = db.get_plan(plan_id)
                if plan:
                    text = f"<b>📝 Editing: {plan['name']}</b>\n\n"
                    text += f"💰 Price: ₹{int(plan['price'])}\n"
                    text += f"📅 Validity: {plan['validity_days']} days\n"
                    text += f"🔗 Link: {plan.get('channel_link', 'Not set')}\n"
                    desc = plan.get('description', 'Not set')
                    text += f"📝 Content: {desc}\n"
                    text += f"📎 Media: {len(db.get_plan_media(plan_id))} items"
                    safe_edit(call.message.chat.id, call.message.message_id, text,
                             reply_markup=edit_plan_keyboard(plan_id))
        
        # ========== ADMIN SETTINGS ==========
        elif data == "admin_welcome_img":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'welcome_image'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "🖼️ Send new welcome image:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        elif data == "admin_welcome_video":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'welcome_video'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "🎬 Send new welcome video:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        # ========== ADMIN WELCOME VIDEOS (NEW) ==========
        elif data == "admin_welcome_videos":
            if is_admin(user_id):
                videos = db.get_welcome_videos()
                text = f"<b>🎬 Welcome Videos</b> ({len(videos)}/5)\n\n"
                if videos:
                    for v in videos:
                        text += f"#{v['order_num']} • Video ID: <code>{v['file_id'][:10]}...</code>\n"
                else:
                    text += "No videos set yet. Send up to 5 videos."
                
                safe_edit(call.message.chat.id, call.message.message_id, text,
                         reply_markup=welcome_videos_keyboard())
        
        elif data == "add_welcome_video":
            if is_admin(user_id):
                videos = db.get_welcome_videos()
                if len(videos) >= 5:
                    bot.answer_callback_query(call.id, "❌ Maximum 5 videos allowed!")
                    return
                user_data[user_id] = {'add_welcome_video': True}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📤 Send a video to add as welcome video.\n\n"
                         f"📊 Current: {len(videos)}/5 videos",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_welcome_videos")))
        
        elif data.startswith("del_welcome_vid_"):
            if is_admin(user_id):
                vid = int(data.split("_")[3])
                db.delete_welcome_video(vid)
                bot.answer_callback_query(call.id, "🗑️ Video deleted!")
                videos = db.get_welcome_videos()
                text = f"<b>🎬 Welcome Videos</b> ({len(videos)}/5)\n\n"
                if videos:
                    for v in videos:
                        text += f"#{v['order_num']} • Video ID: <code>{v['file_id'][:10]}...</code>\n"
                else:
                    text += "No videos set yet. Send up to 5 videos."
                safe_edit(call.message.chat.id, call.message.message_id, text,
                         reply_markup=welcome_videos_keyboard())
        
        elif data == "clear_welcome_videos":
            if is_admin(user_id):
                db.clear_welcome_videos()
                bot.answer_callback_query(call.id, "🗑️ All videos cleared!")
                text = "<b>🎬 Welcome Videos</b> (0/5)\n\nNo videos set yet. Send up to 5 videos."
                safe_edit(call.message.chat.id, call.message.message_id, text,
                         reply_markup=welcome_videos_keyboard())
        
        elif data == "admin_welcome_text":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'welcome_text'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📝 Send new welcome text:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        elif data == "admin_upi":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'upi_id'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "💰 Send UPI ID:\n\nExample: premium@upi",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        elif data == "admin_bot_name":
            if is_admin(user_id):
                user_data[user_id] = {'setting': 'bot_name'}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "🏷️ Send new bot name:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        elif data == "admin_broadcast":
            if is_admin(user_id):
                user_data[user_id] = {'broadcast': True}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📢 <b>Broadcast Message</b>\n\nSend message, photo, video, or document to ALL users:",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        # ========== EXPORT DATABASE ==========
        elif data == "admin_export_db":
            if is_admin(user_id):
                try:
                    export_data = db.export_database()
                    json_str = json.dumps(export_data, indent=2, default=str)
                    file_data = io.BytesIO(json_str.encode('utf-8'))
                    file_data.name = f"database_export_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"
                    
                    bot.send_document(user_id, file_data, caption="📤 Database export complete!\n\n✅ Users data\n✅ Plans with media\n✅ Welcome videos")
                    
                    safe_edit(call.message.chat.id, call.message.message_id,
                             f"<b>⚙️ Admin Panel</b>\n\n📤 Database exported successfully!",
                             reply_markup=admin_keyboard())
                    bot.answer_callback_query(call.id, "✅ Export complete!")
                except Exception as e:
                    logger.error(f"Export error: {e}")
                    bot.answer_callback_query(call.id, f"❌ Export error: {str(e)}")
        
        # ========== IMPORT DATABASE ==========
        elif data == "admin_import_db":
            if is_admin(user_id):
                user_data[user_id] = {'import_db': True}
                safe_edit(call.message.chat.id, call.message.message_id,
                         "📥 <b>Import Database</b>\n\nSend the JSON file you exported earlier.\n\n⚠️ This will replace existing Users and Plans data!",
                         reply_markup=types.InlineKeyboardMarkup().add(
                         types.InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")))
        
        # ========== ADMIN PAYMENTS ==========
        elif data == "admin_payments":
            if is_admin(user_id):
                pending = db.get_pending_payments()
                if pending:
                    kb = types.InlineKeyboardMarkup(row_width=1)
                    for p in pending:
                        name = p.get('username') or p.get('first_name', 'Unknown')
                        kb.add(types.InlineKeyboardButton(f"🕐 {name} - ₹{int(p['amount'])}",
                                 callback_data=f"pview_{p['payment_id']}"))
                    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
                    safe_edit(call.message.chat.id, call.message.message_id, 
                             f"<b>💳 Pending Payments</b> ({len(pending)})", reply_markup=kb)
                else:
                    kb = types.InlineKeyboardMarkup(row_width=1)
                    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_panel"))
                    safe_edit(call.message.chat.id, call.message.message_id,
                             "✅ No pending payments", reply_markup=kb)
        
        elif data.startswith("pview_"):
            if is_admin(user_id):
                pid = int(data.split("_")[1])
                payment = db.get_payment(pid)
                if payment:
                    user = db.get_user(payment['user_id'])
                    plan = db.get_plan(payment['plan_id'])
                    text = f"<b>💳 Payment #{pid}</b>\n\n"
                    text += f"👤 User: {user.get('first_name', 'Unknown')}\n"
                    text += f"🆔 Chat ID: <code>{payment['user_id']}</code>\n"
                    text += f"📋 Plan: {plan['name'] if plan else 'Unknown'}\n"
                    text += f"💰 Amount: ₹{int(payment['amount'])}\n"
                    text += f"📅 Date: {payment['created_at'][:16]}\n"
                    text += f"📌 Status: <b>{payment['status'].upper()}</b>"
                    
                    kb = types.InlineKeyboardMarkup(row_width=2)
                    if payment['status'] == 'pending':
                        kb.row(
                            types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{pid}"),
                            types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{pid}")
                        )
                    kb.add(types.InlineKeyboardButton("🔙 Back", callback_data="admin_payments"))
                    safe_edit(call.message.chat.id, call.message.message_id, text, reply_markup=kb)
                    if payment.get('screenshot_file_id'):
                        safe_photo(user_id, payment['screenshot_file_id'], "📱 Payment Screenshot")
                else:
                    bot.answer_callback_query(call.id, "Payment not found!")
        
        # ========== APPROVE PAYMENT ==========
        elif data.startswith("approve_"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Unauthorized!")
                return
            
            try:
                pid = int(data.split("_")[1])
                logger.info(f"Admin {user_id} approving payment {pid}")
                
                success, result = db.approve_payment(pid)
                
                if not success:
                    bot.answer_callback_query(call.id, f"❌ {result}")
                    return
                
                payment = db.get_payment(pid)
                if payment:
                    user = db.get_user(payment['user_id'])
                    plan = db.get_plan(payment['plan_id'])
                    
                    if user and plan:
                        # Send the approved buyer ONLY the access-granted message + plan channel link.
                        send_payment_access_granted(payment['user_id'], plan)

                    # Notify every other registered user about the new purchase.
                    # IMPORTANT: the approved buyer is excluded so they do not receive
                    # their own "NEW PURCHASE SUCCESS" notification.
                    try:
                        broadcast_new_purchase(
                            plan['name'] if plan else 'a premium plan',
                            payment.get('screenshot_file_id') if payment else None,
                            exclude_user_id=payment.get('user_id') if payment else None
                        )
                    except Exception as e:
                        logger.error(f"New purchase broadcast failed for payment #{pid}: {e}")

                    # Automatically publish the approved payment to the configured Proof Channel.
                    threading.Thread(
                        target=publish_payment_proof,
                        args=(payment, plan),
                        daemon=True
                    ).start()
                
                bot.answer_callback_query(call.id, "✅ Payment Approved Successfully!")
                logger.info(f"Payment {pid} approved by admin {user_id}")
                
                refresh_payment_list(call.message.chat.id, call.message.message_id, user_id)
                
            except ValueError as e:
                logger.error(f"Approve payment error: {e}")
                bot.answer_callback_query(call.id, "❌ Invalid payment ID!")
            except Exception as e:
                logger.error(f"Approve payment error: {e}")
                bot.answer_callback_query(call.id, "❌ Error approving payment!")
        
        # ========== REJECT PAYMENT ==========
        elif data.startswith("reject_"):
            if not is_admin(user_id):
                bot.answer_callback_query(call.id, "❌ Unauthorized!")
                return
            
            try:
                pid = int(data.split("_")[1])
                logger.info(f"Admin {user_id} rejecting payment {pid}")
                
                user_data[user_id] = {'reject_payment': pid, 'reject_message_id': call.message.message_id}
                bot.answer_callback_query(call.id, "📝 Please send rejection reason:")
                bot.send_message(user_id, "📝 Send the rejection reason for this payment:")
                
            except ValueError as e:
                logger.error(f"Reject payment error: {e}")
                bot.answer_callback_query(call.id, "❌ Invalid payment ID!")
            except Exception as e:
                logger.error(f"Reject payment error: {e}")
                bot.answer_callback_query(call.id, "❌ Error rejecting payment!")
        
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Error occurred!")
        except:
            pass

# ==================== MESSAGE HANDLERS ====================

@bot.message_handler(content_types=['photo'])
def handle_photo(msg):
    user_id = msg.from_user.id
    file_id = msg.photo[-1].file_id
    caption = msg.caption or ""
    
    # Screenshot upload
    if user_id in user_data and 'screenshot_plan' in user_data[user_id]:
        plan_id = user_data[user_id]['screenshot_plan']
        plan = db.get_plan(plan_id)
        if plan:
            state = user_data.get(user_id, {})
            pid = db.add_payment(user_id, plan_id, plan['price'], file_id, state.get('cf_order_id', ''), state.get('cf_payment_id', ''))
            bot.reply_to(msg, "✅ Payment screenshot received!\nAdmin will review shortly.")
            
            payment = db.get_payment(pid)
            if payment:
                user = db.get_user(user_id)
                text = f"<b>💳 New Payment</b>\n\n"
                text += f"👤 User: {user.get('first_name', 'Unknown')}\n"
                text += f"🆔 Chat ID: <code>{user_id}</code>\n"
                text += f"📋 Plan: {plan['name']}\n"
                text += f"💰 Amount: ₹{int(plan['price'])}\n"
                if caption:
                    text += f"📝 UTR: {caption}\n"
                text += f"🆔 Payment ID: #{pid}\n"
                if payment.get('cf_order_id'):
                    text += f"🏦 Cashfree Order: <code>{payment['cf_order_id']}</code>\n"
                if payment.get('cf_payment_id'):
                    text += f"🔖 Cashfree Payment: <code>{payment['cf_payment_id']}</code>"
                
                kb = types.InlineKeyboardMarkup(row_width=2)
                kb.row(
                    types.InlineKeyboardButton("✅ Approve", callback_data=f"approve_{pid}"),
                    types.InlineKeyboardButton("❌ Reject", callback_data=f"reject_{pid}")
                )
                
                for admin in ADMIN_IDS:
                    try:
                        bot.send_photo(admin, file_id, caption=text, reply_markup=kb)
                        logger.info(f"Payment notification sent to admin {admin}")
                    except Exception as e:
                        logger.error(f"Failed to send notification to admin {admin}: {e}")
            del user_data[user_id]
        return
    
    # Proof media upload
    if user_id in user_data and user_data[user_id].get('add_proof'):
        if not is_admin(user_id):
            del user_data[user_id]
            return
        success, result = db.add_proof_media('photo', file_id)
        bot.reply_to(msg, ('✅ ' if success else '❌ ') + result)
        del user_data[user_id]
        if success:
            proofs = db.get_proof_media()
            safe_send(user_id, f"📸 <b>Proof Settings</b>\n\nProofs: {len(proofs)}/4", reply_markup=proof_settings_keyboard())
        return

    # Settings - Image
    if user_id in user_data and 'setting' in user_data[user_id]:
        key = user_data[user_id]['setting']
        db.set_setting(key, file_id)
        if key == 'welcome_image':
            global WELCOME_IMAGE
            WELCOME_IMAGE = file_id
        bot.reply_to(msg, f"✅ {key.replace('_', ' ').title()} updated!")
        del user_data[user_id]
        return
    
    # Add media to plan
    if user_id in user_data and 'add_media' in user_data[user_id]:
        plan_id = user_data[user_id]['add_media']
        count = user_data[user_id].get('media_count', 0)
        if count < 5:
            db.add_media(plan_id, 'photo', file_id)
            user_data[user_id]['media_count'] = count + 1
            remaining = 5 - (count + 1)
            if remaining > 0:
                bot.reply_to(msg, f"✅ Photo added! ({count+1}/5)\nSend {remaining} more media or click Done.")
            else:
                bot.reply_to(msg, "✅ All 5 media added! Click Done.")
        else:
            bot.reply_to(msg, "❌ Already 5 media added! Click Done to finish.")
        return
    
    # Broadcast
    if user_id in user_data and user_data[user_id].get('broadcast'):
        users = db.get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_photo(u['user_id'], file_id, caption=msg.caption or '')
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(msg, f"✅ Broadcast sent to {sent} users!")
        del user_data[user_id]

@bot.message_handler(content_types=['video'])
def handle_video(msg):
    user_id = msg.from_user.id
    file_id = msg.video.file_id
    
    # Proof media upload
    if user_id in user_data and user_data[user_id].get('add_proof'):
        if not is_admin(user_id):
            del user_data[user_id]
            return
        success, result = db.add_proof_media('video', file_id)
        bot.reply_to(msg, ('✅ ' if success else '❌ ') + result)
        del user_data[user_id]
        if success:
            proofs = db.get_proof_media()
            safe_send(user_id, f"📸 <b>Proof Settings</b>\n\nProofs: {len(proofs)}/4", reply_markup=proof_settings_keyboard())
        return

    # Add welcome video (NEW)
    if user_id in user_data and user_data[user_id].get('add_welcome_video'):
        success, result = db.add_welcome_video(file_id)
        bot.reply_to(msg, result)
        if success:
            videos = db.get_welcome_videos()
            text = f"<b>🎬 Welcome Videos</b> ({len(videos)}/5)\n\n"
            if videos:
                for v in videos:
                    text += f"#{v['order_num']} • Video ID: <code>{v['file_id'][:10]}...</code>\n"
            else:
                text += "No videos set yet. Send up to 5 videos."
            safe_send(user_id, text, reply_markup=welcome_videos_keyboard())
        del user_data[user_id]
        return
    
    # Settings - Video
    if user_id in user_data and 'setting' in user_data[user_id]:
        key = user_data[user_id]['setting']
        if key == 'welcome_video':
            db.set_setting('welcome_video', file_id)
            global WELCOME_VIDEO
            WELCOME_VIDEO = file_id
            bot.reply_to(msg, "✅ Welcome video updated!")
            del user_data[user_id]
        return
    
    # Add media to plan
    if user_id in user_data and 'add_media' in user_data[user_id]:
        plan_id = user_data[user_id]['add_media']
        count = user_data[user_id].get('media_count', 0)
        if count < 5:
            db.add_media(plan_id, 'video', file_id)
            user_data[user_id]['media_count'] = count + 1
            remaining = 5 - (count + 1)
            if remaining > 0:
                bot.reply_to(msg, f"✅ Video added! ({count+1}/5)\nSend {remaining} more media or click Done.")
            else:
                bot.reply_to(msg, "✅ All 5 media added! Click Done.")
        else:
            bot.reply_to(msg, "❌ Already 5 media added! Click Done to finish.")
        return
    
    # Broadcast
    if user_id in user_data and user_data[user_id].get('broadcast'):
        users = db.get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_video(u['user_id'], file_id, caption=msg.caption or '')
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(msg, f"✅ Broadcast sent to {sent} users!")
        del user_data[user_id]

@bot.message_handler(content_types=['document'])
def handle_document(msg):
    user_id = msg.from_user.id
    file_id = msg.document.file_id
    file_name = msg.document.file_name or ''
    
    # Import Database
    if user_id in user_data and user_data[user_id].get('import_db'):
        if file_name.endswith('.json'):
            try:
                file_info = bot.get_file(file_id)
                downloaded_file = bot.download_file(file_info.file_path)
                json_str = downloaded_file.decode('utf-8')
                import_data = json.loads(json_str)
                
                success, error = db.import_database(import_data)
                
                if success:
                    global WELCOME_IMAGE, WELCOME_VIDEO, WELCOME_TEXT, BOT_NAME, UPI_ID
                    WELCOME_IMAGE = db.get_setting('welcome_image')
                    WELCOME_VIDEO = db.get_setting('welcome_video')
                    WELCOME_TEXT = db.get_setting('welcome_text')
                    BOT_NAME = db.get_setting('bot_name')
                    UPI_ID = db.get_setting('upi_id')
                    
                    bot.reply_to(msg, "✅ Database imported successfully!\n\n✅ Users restored\n✅ Plans with media restored\n✅ Welcome videos restored")
                    
                    try:
                        safe_send(user_id, f"<b>⚙️ Admin Panel</b>\n\n📥 Import complete!", reply_markup=admin_keyboard())
                    except:
                        pass
                else:
                    bot.reply_to(msg, f"❌ Import failed: {error}")
                
                del user_data[user_id]
                
            except json.JSONDecodeError as e:
                bot.reply_to(msg, f"❌ Invalid JSON file: {str(e)}")
                del user_data[user_id]
            except Exception as e:
                logger.error(f"Import error: {e}")
                bot.reply_to(msg, f"❌ Import error: {str(e)}")
                del user_data[user_id]
        else:
            bot.reply_to(msg, "❌ Please send a JSON file (exported from this bot).")
        return
    
    # Broadcast - Document
    if user_id in user_data and user_data[user_id].get('broadcast'):
        users = db.get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_document(u['user_id'], file_id, caption=msg.caption or '')
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(msg, f"✅ Broadcast sent to {sent} users!")
        del user_data[user_id]

@bot.message_handler(content_types=['audio', 'voice', 'animation', 'sticker'])
def handle_other_media(msg):
    user_id = msg.from_user.id
    
    if user_id in user_data and user_data[user_id].get('broadcast'):
        users = db.get_all_users()
        sent = 0
        for u in users:
            try:
                if msg.content_type == 'audio':
                    bot.send_audio(u['user_id'], msg.audio.file_id, caption=msg.caption or '')
                elif msg.content_type == 'voice':
                    bot.send_voice(u['user_id'], msg.voice.file_id)
                elif msg.content_type == 'animation':
                    bot.send_animation(u['user_id'], msg.animation.file_id, caption=msg.caption or '')
                elif msg.content_type == 'sticker':
                    bot.send_sticker(u['user_id'], msg.sticker.file_id)
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(msg, f"✅ Broadcast sent to {sent} users!")
        del user_data[user_id]

@bot.message_handler(func=lambda m: True, content_types=['text'])
def handle_text(msg):
    user_id = msg.from_user.id
    
    # ===== REJECT PAYMENT REASON =====
    if user_id in user_data and 'reject_payment' in user_data[user_id]:
        pid = user_data[user_id]['reject_payment']
        reason = msg.text
        msg_id = user_data[user_id].get('reject_message_id')
        
        logger.info(f"Admin {user_id} rejecting payment {pid} with reason: {reason}")
        
        success, result = db.reject_payment(pid, reason)
        
        if not success:
            bot.reply_to(msg, f"❌ {result}")
            del user_data[user_id]
            return
        
        payment = db.get_payment(pid)
        if payment:
            user = db.get_user(payment['user_id'])
            plan = db.get_plan(payment['plan_id'])
            if user and plan:
                text = f"❌ <b>Payment Rejected</b>\n\n"
                text += f"📋 Plan: {plan['name']}\n"
                text += f"💰 Amount: ₹{int(plan['price'])}\n"
                text += f"📝 Reason: {reason}\n\n"
                text += "Please try again with correct payment."
                bot.send_message(payment['user_id'], text)
                logger.info(f"User {payment['user_id']} notified about payment rejection")
        
        bot.reply_to(msg, "✅ Payment rejected and user notified!")
        logger.info(f"Payment {pid} rejected by admin {user_id}")
        
        if msg_id:
            try:
                refresh_payment_list(msg.chat.id, msg_id, user_id)
            except:
                pass
        
        del user_data[user_id]
        return
    
    # ===== ADD PLAN =====
    if user_id in user_data and user_data[user_id].get('add_plan'):
        step = user_data[user_id].get('step')
        
        if step == 'name':
            user_data[user_id]['pname'] = msg.text
            user_data[user_id]['step'] = 'price'
            bot.reply_to(msg, "Step 2/6: Enter price (in ₹):")
        
        elif step == 'price':
            try:
                user_data[user_id]['pprice'] = float(msg.text)
                user_data[user_id]['step'] = 'validity'
                bot.reply_to(msg, "Step 3/6: Enter validity (in days):")
            except:
                bot.reply_to(msg, "❌ Invalid price! Enter number:")
        
        elif step == 'validity':
            try:
                user_data[user_id]['pvalidity'] = int(msg.text)
                user_data[user_id]['step'] = 'link'
                bot.reply_to(msg, "Step 4/6: Enter channel link:\n\nExample: https://t.me/yourchannel")
            except:
                bot.reply_to(msg, "❌ Invalid days! Enter number:")
        
        elif step == 'link':
            user_data[user_id]['plink'] = msg.text
            user_data[user_id]['step'] = 'description'
            bot.reply_to(msg, "Step 5/6: Enter plan description (content details users will see):")
        
        elif step == 'description':
            user_data[user_id]['pdescription'] = msg.text
            user_data[user_id]['step'] = 'done'
            bot.reply_to(msg, "✅ Plan created!\n\nNow send 5 videos/photos for this plan.\nSend media one by one.")
            
            plan_id = db.add_plan(
                user_data[user_id]['pname'],
                user_data[user_id]['pprice'],
                user_data[user_id]['pvalidity'],
                user_data[user_id]['plink'],
                user_data[user_id]['pdescription']
            )
            user_data[user_id]['add_media'] = plan_id
            user_data[user_id]['media_count'] = 0
            del user_data[user_id]['add_plan']
            del user_data[user_id]['step']
        
        return
    
    # ===== EDIT PLAN =====
    if user_id in user_data and 'edit_plan' in user_data[user_id]:
        plan_id = user_data[user_id]['edit_plan']
        field = user_data[user_id]['field']
        
        if field == 'name':
            db.update_plan(plan_id, name=msg.text)
            bot.reply_to(msg, f"✅ Plan name updated to: {msg.text}")
        elif field == 'price':
            try:
                db.update_plan(plan_id, price=float(msg.text))
                bot.reply_to(msg, f"✅ Price updated to: ₹{msg.text}")
            except:
                bot.reply_to(msg, "❌ Invalid price!")
        elif field == 'validity':
            try:
                db.update_plan(plan_id, validity_days=int(msg.text))
                bot.reply_to(msg, f"✅ Validity updated to: {msg.text} days")
            except:
                bot.reply_to(msg, "❌ Invalid days!")
        elif field == 'link':
            db.update_plan(plan_id, channel_link=msg.text)
            bot.reply_to(msg, f"✅ Channel link updated!")
        elif field == 'description':
            db.update_plan(plan_id, description=msg.text)
            bot.reply_to(msg, f"✅ Content description updated!")
        
        del user_data[user_id]
        return
    
    # ===== SETTINGS =====
    if user_id in user_data and 'setting' in user_data[user_id]:
        key = user_data[user_id]['setting']
        
        if key == 'welcome_text':
            db.set_setting('welcome_text', msg.text)
            global WELCOME_TEXT
            WELCOME_TEXT = msg.text
            bot.reply_to(msg, "✅ Welcome text updated!")
        elif key == 'upi_id':
            db.set_setting('upi_id', msg.text)
            global UPI_ID
            UPI_ID = msg.text
            bot.reply_to(msg, "✅ UPI ID updated!")
        elif key == 'bot_name':
            db.set_setting('bot_name', msg.text)
            global BOT_NAME
            BOT_NAME = msg.text
            bot.reply_to(msg, f"✅ Bot name updated to: {msg.text}")
        elif key == 'proof_channel_id':
            db.set_setting('proof_channel_id', msg.text.strip())
            bot.reply_to(msg, "✅ Proof channel ID / username updated!")
        elif key == 'proof_channel_link':
            db.set_setting('proof_channel_link', msg.text.strip())
            bot.reply_to(msg, "✅ Proof channel link updated!")
        elif key == 'proof_description':
            db.set_setting('proof_description', msg.text)
            bot.reply_to(msg, "✅ Proof channel description updated!")
        
        del user_data[user_id]
        return
    
    # ===== BROADCAST =====
    if user_id in user_data and user_data[user_id].get('broadcast'):
        users = db.get_all_users()
        sent = 0
        for u in users:
            try:
                bot.send_message(u['user_id'], msg.text)
                sent += 1
                time.sleep(0.05)
            except:
                pass
        bot.reply_to(msg, f"✅ Broadcast sent to {sent} users!")
        del user_data[user_id]

# ==================== MAIN ====================
def run_bot():
    while bot_running:
        try:
            logger.info("🤖 Bot polling started...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            if bot_running:
                time.sleep(5)

def main():
    logger.info("🚀 Starting Premium Bot...")
    try:
        bot.get_me()
        logger.info("✅ Bot connected")
        
        http_thread = threading.Thread(target=run_http, daemon=True)
        http_thread.start()
        logger.info(f"🌐 HTTP: http://0.0.0.0:{PORT}")
        
        run_bot()
    except KeyboardInterrupt:
        logger.info("🛑 Stopping...")
        global bot_running
        bot_running = False
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
