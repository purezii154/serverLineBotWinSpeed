import os
import re
import json
import sqlite3
import urllib.parse
from urllib.parse import parse_qsl
from flask import Flask, request, abort, g
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (Configuration, ApiClient, MessagingApi,
                                  ReplyMessageRequest, FlexMessage, FlexContainer, TextMessage)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, PostbackEvent
from linebot.v3.exceptions import InvalidSignatureError
import pyodbc
from dotenv import load_dotenv
import requests

# ==========================================
# 🛡️ 1. โหลด Config และตั้งค่า Server
# ==========================================
load_dotenv(override=True)

SERVER_LIST = {
    "SRV_NEW": {
        "name": "เซิร์ฟเวอร์เชียงใหม่(26633)",
        "ip": "cmprosoft.fortiddns.com,26633",
        "uid": "sa",
        "pwd": "Admin@prosoft"
    },
    "SRV_OLD": {
        "name": "เซิร์ฟเวอร์ กทม. (14033)",
        "ip": "prosoft.gotdns.com,14033",
        "uid": "prosoftsa",
        "pwd": "Prosoft@12345"
    }
}

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')

app = Flask(__name__)
configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ==========================================
# 🤖 ฟังก์ชันยิง API ของ Groq โดยตรง (แทนไลบรารีเดิมที่พัง)
# ==========================================
def get_ai_response(prompt):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0
    }
    response = requests.post(url, headers=headers, json=data)
    return response.json()['choices'][0]['message']['content']

# ==========================================
# 💂‍♂️ 2. ระบบ Session โลคอล (SQLite)
# ==========================================
DB_SESSION_FILE = "sessions.db"

def init_sqlite_db():
    conn = sqlite3.connect(DB_SESSION_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS UserSessions (
            LineUserID TEXT PRIMARY KEY,
            SelectedServer TEXT NULL,
            SelectedDB TEXT NULL,
            IsLoggedIn INTEGER DEFAULT 0,
            LastActive DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def set_login_session(user_id):
    conn = sqlite3.connect(DB_SESSION_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM UserSessions WHERE LineUserID = ?", (user_id,))
    if cursor.fetchone():
        cursor.execute("UPDATE UserSessions SET IsLoggedIn = 1, SelectedServer = NULL, SelectedDB = NULL, LastActive = CURRENT_TIMESTAMP WHERE LineUserID = ?", (user_id,))
    else:
        cursor.execute("INSERT INTO UserSessions (LineUserID, IsLoggedIn) VALUES (?, 1)", (user_id,))
    conn.commit()
    conn.close()

def save_selected_server(user_id, server_key):
    conn = sqlite3.connect(DB_SESSION_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE UserSessions SET SelectedServer = ?, SelectedDB = NULL, LastActive = CURRENT_TIMESTAMP WHERE LineUserID = ?", (server_key, user_id))
    conn.commit()
    conn.close()

def save_selected_db(user_id, db_name):
    conn = sqlite3.connect(DB_SESSION_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE UserSessions SET SelectedDB = ?, LastActive = CURRENT_TIMESTAMP WHERE LineUserID = ?", (db_name, user_id))
    conn.commit()
    conn.close()

def get_session(user_id):
    conn = sqlite3.connect(DB_SESSION_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT IsLoggedIn, SelectedServer, SelectedDB FROM UserSessions WHERE LineUserID = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"is_logged_in": bool(row[0]), "selected_server": row[1], "selected_db": row[2]}
    return None

def delete_session(user_id):
    conn = sqlite3.connect(DB_SESSION_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM UserSessions WHERE LineUserID = ?", (user_id,))
    conn.commit()
    conn.close()

# ==========================================
# 🗄️ 3. การเชื่อมต่อแบบ Dynamic Connection
# ==========================================
def get_connection(db_name="master"):
    server_key = getattr(g, 'server_key', 'SRV_NEW')
    srv = SERVER_LIST.get(server_key)
    
    if not srv:
        print(f"❌ ไม่พบข้อมูลการตั้งค่าสำหรับ Server: {server_key}")
        return None

    driver = os.environ.get('CENTRAL_DB_DRIVER', '{ODBC Driver 17 for SQL Server}')
    conn_str = f"DRIVER={driver};SERVER={srv['ip']};DATABASE={db_name};UID={srv['uid']};PWD={srv['pwd']}"
    
    return pyodbc.connect(conn_str)

# ==========================================
# 📑 4. ฟังก์ชันจัดการรายชื่อ Database และ Server
# ==========================================
def get_all_dbs(search_term=''):
    conn = get_connection("master")
    if not conn: return []
    cursor = conn.cursor()
    query = f"""
        SELECT name FROM sys.databases 
        WHERE name NOT IN ('master', 'tempdb', 'model', 'msdb', 'ReportServer', 'ReportServerTempDB')
        AND name LIKE ?
        ORDER BY name
    """
    cursor.execute(query, ('%' + search_term + '%',))
    dbs = [row[0] for row in cursor.fetchall()]
    conn.close()
    return dbs

def build_server_carousel():
    contents = []
    for key, srv in SERVER_LIST.items():
        contents.append({
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "20px",
                "contents": [
                    {"type": "text", "text": "🖥️ เลือกเซิร์ฟเวอร์", "weight": "bold", "color": "#1A237E", "size": "sm"},
                    {"type": "text", "text": srv['name'], "weight": "bold", "size": "xl", "margin": "md", "wrap": True},
                    {"type": "text", "text": f"IP: {srv['ip'].split(',')[0]}", "size": "xs", "color": "#888888", "margin": "sm"}
                ]
            },
            "footer": {
                "type": "box", "layout": "vertical",
                "contents": [
                    {
                        "type": "button", "style": "primary", "color": "#1A237E",
                        "action": {"type": "message", "label": "เข้าสู่ระบบนี้", "text": f"select_server:{key}"}
                    }
                ]
            }
        })
    return {"type": "carousel", "contents": contents}

def build_carousel_db_list(db_list, search_term=''):
    if not db_list:
        return {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "contents": [{"type": "text", "text": f"❌ ไม่พบฐานข้อมูลที่ตรงกับ '{search_term}'", "color": "#FF0000"}]}
        }

    bubbles = []
    chunk_size = 8 
    chunks = [db_list[i:i + chunk_size] for i in range(0, len(db_list), chunk_size)]

    for i, chunk in enumerate(chunks[:12]):
        contents = []
        for db in chunk:
            is_match = bool(search_term) and search_term.lower() in db.lower()
            box_border_color = "#FF0000" if is_match else "#00000000"
            box_border_width = "2px" if is_match else "0px"
            text_color = "#FF0000" if is_match else "#1A237E"

            contents.append({
                "type": "box", "layout": "vertical", "margin": "sm", "paddingAll": "10px",
                "backgroundColor": "#F4F6F9" if not is_match else "#FFF0F0",
                "cornerRadius": "8px", "borderColor": box_border_color, "borderWidth": box_border_width,
                "action": {"type": "postback", "data": f"action=select_db&db_name={db}", "displayText": f"เลือก {db}"},
                "contents": [{"type": "text", "text": db, "size": "sm", "color": text_color, "weight": "bold", "wrap": True}]
            })
        
        footer_text = "ปัดซ้าย-ขวา เพื่อดูหน้าถัดไป"
        if len(db_list) > 96 and i == 11:
            footer_text = "⚠️ แสดงสูงสุด 96 รายการ โปรดค้นหาให้แคบลง"

        bubbles.append({
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "15px",
                "contents": [
                    {"type": "text", "text": f"🗄️ เลือกฐานข้อมูล ({i+1}/{len(chunks[:12])})", "weight": "bold", "size": "md", "color": "#1A237E"},
                    {"type": "text", "text": footer_text, "size": "xs", "color": "#888888", "margin": "sm"},
                    {"type": "separator", "margin": "md"},
                    {"type": "box", "layout": "vertical", "margin": "md", "contents": contents}
                ]
            }
        })
    return {"type": "carousel", "contents": bubbles}

# ==========================================
# 📊 5. ฟังก์ชันดึงข้อมูล Database & AI Search
# ==========================================
def get_stock(item_code, db_name):
    conn = get_connection(db_name)
    if not conn: return None, None
    cursor = conn.cursor()
    cursor.execute("""
        SELECT i.ItemCode, i.ItemName, SUM(s.ItemQty) as TotalQty
        FROM whStockOnHand s JOIN whItem i ON s.ItemID = i.ItemID
        WHERE i.ItemCode = ? GROUP BY i.ItemCode, i.ItemName
    """, item_code)
    main = cursor.fetchone()
    if not main:
        conn.close()
        return None, None
    cursor.execute("""
        SELECT l.LotNumber, SUM(s.ItemQty) as OnHand, 0 as Reserved, SUM(s.ItemQty) as Available
        FROM whStockOnHand s JOIN whItem i ON s.ItemID = i.ItemID
        LEFT JOIN whLotAndSerialHD l ON s.LotAndSerialID = l.LotAndSerialID
        WHERE i.ItemCode = ? GROUP BY l.LotNumber ORDER BY l.LotNumber DESC
    """, item_code)
    lots = cursor.fetchall()
    conn.close()
    return main, lots

def get_recent_orders(db_name, limit=5):
    conn = get_connection(db_name)
    if not conn: return []
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP (?) soh.DocuNo, soh.DocuDate, soh.TotalAmnt, c.CustomerName
        FROM csSalesOrderHD soh JOIN csCustomer c ON soh.CustomerID = c.CustomerID
        ORDER BY soh.DocuDate DESC, soh.DocuNo DESC
    """, limit)
    orders = cursor.fetchall()
    conn.close()
    return orders

def get_recent_customer_pos(db_name, limit=5):
    conn = get_connection(db_name)
    if not conn: return []
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP (?) soh.CustomerPONo, soh.CustomerPODate, soh.TotalAmnt, soh.DocuNo, c.CustomerName
        FROM csSalesOrderHD soh JOIN csCustomer c ON soh.CustomerID = c.CustomerID
        WHERE soh.CustomerPONo IS NOT NULL AND soh.CustomerPONo != ''
        ORDER BY soh.DocuDate DESC, soh.DocuNo DESC
    """, limit)
    pos = cursor.fetchall()
    conn.close()
    return pos

def get_order_details(doc_no, db_name):
    conn = get_connection(db_name)
    if not conn: return []
    cursor = conn.cursor()
    cursor.execute("""
        SELECT soh.DocuNo, soh.DocuDate, soh.TotalAmnt, i.ItemCode, i.ItemName, sod.ItemQty, sod.UnitPrice, (sod.ItemQty * sod.UnitPrice) AS Amnt
        FROM csSalesOrderDT sod JOIN csSalesOrderHD soh ON sod.RelateID = soh.SalesOrderID
        JOIN whItem i ON sod.ItemID = i.ItemID WHERE soh.DocuNo = ?
    """, doc_no)
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_last_price(item_code, db_name):
    conn = get_connection(db_name)
    if not conn: return None
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP 1 soh.DocuDate, i.ItemCode, i.ItemName, sod.UnitPrice, c.CustomerName
        FROM csSalesOrderDT sod JOIN csSalesOrderHD soh ON sod.RelateID = soh.SalesOrderID
        JOIN csCustomer c ON soh.CustomerID = c.CustomerID JOIN whItem i ON sod.ItemID = i.ItemID
        WHERE i.ItemCode = ? ORDER BY soh.DocuDate DESC
    """, item_code)
    row = cursor.fetchone()
    conn.close()
    return row

def get_stock_summary(db_name):
    conn = get_connection(db_name)
    if not conn: return []
    cursor = conn.cursor()
    cursor.execute("""
        SELECT TOP 10 i.ItemCode, i.ItemName, SUM(s.ItemQty) as TotalQty
        FROM whStockOnHand s JOIN whItem i ON s.ItemID = i.ItemID
        GROUP BY i.ItemCode, i.ItemName ORDER BY TotalQty DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return rows

# ==========================================
# 🔍 ฟังก์ชันค้นหาสินค้าอัจฉริยะ (AI ทำงาน 2 ชั้น)
# ==========================================
def ai_search(user_text, db_name):
    conn = get_connection(db_name)
    if not conn: return []
    cursor = conn.cursor()
    safe_text = user_text.replace("'", "''") 

    # 🌟 Step 1: ให้ AI ดึงคีย์เวิร์ด
    try:
        prompt_kw = f'''ดึงคีย์เวิร์ดสำคัญและแก้คำผิดจากคำค้นหานี้: "{safe_text}"
(ตัวอย่าง: "Moonsoon" -> "Monsoon", "ลำใย" -> "ลำไย", "ไวน์องุ่นsunny" -> "ไวน์องุ่น sunny")
ตอบกลับเฉพาะคีย์เวิร์ดที่แยกด้วยช่องว่าง ห้ามพิมพ์อธิบาย'''
        
        ai_text = get_ai_response(prompt_kw).strip()
        ai_text = ai_text.replace('"', '').replace("'", "").replace(",", " ")
        keywords = ai_text.split()
    except:
        keywords = safe_text.split()
        
    if not keywords: keywords = [safe_text]

    # 🌟 Step 2: ค้นหาในฐานข้อมูล
    where_clauses = []
    params = []
    for kw in keywords:
        kw_alt = kw.replace('ไ', 'ใ') if 'ไ' in kw else kw.replace('ใ', 'ไ')
        where_clauses.append("(ItemName LIKE N'%' + ? + '%' OR ItemName LIKE N'%' + ? + '%' OR ItemCode LIKE N'%' + ? + '%')")
        params.extend([kw, kw_alt, kw])
        
    query = f"SELECT TOP 20 ItemCode, ItemName FROM whItem WHERE {' AND '.join(where_clauses)} ORDER BY ItemName"
    cursor.execute(query, params)
    items = cursor.fetchall()

    if not items:
        query_or = f"SELECT TOP 20 ItemCode, ItemName FROM whItem WHERE {' OR '.join(where_clauses)} ORDER BY ItemName"
        cursor.execute(query_or, params)
        items = cursor.fetchall()

    conn.close()

    if not items: return []
    clean_items = [(str(row[0]).strip(), str(row[1]).strip()) for row in items]

    if len(clean_items) == 1:
        return [clean_items[0][0]]

    # 🌟 Step 3: เจอหลายรายการ -> ให้ AI ช่วยกรอง
    item_list_str = "\n".join([f"{c}: {n}" for c, n in clean_items])
    prompt_filter = f'''คำค้นหาจากผู้ใช้: "{user_text}"
รายการสินค้าที่พบในระบบ:
{item_list_str}

คำสั่ง:
1. หากผู้ใช้ระบุเจาะจง ให้เลือก "รหัสสินค้าที่ตรงเป๊ะที่สุดเพียง 1 รหัสเท่านั้น"
2. หากคำค้นหาเป็นคำกว้างๆ ให้เลือก "รหัสสินค้าที่เข้าข่ายทั้งหมด"
3. ตอบเฉพาะรหัสสินค้า (ItemCode) ห้ามมีข้อความอธิบายอื่นใดทั้งสิ้น'''

    try:
        filtered_text = get_ai_response(prompt_filter).strip()
        extracted_codes = re.findall(r'[A-Za-z0-9\-]+', filtered_text)
        final_results = [code for code, name in clean_items if code in extracted_codes]
                
        if final_results:
            return final_results[:10]
    except:
        pass

    return [item[0] for item in clean_items[:10]]

# ==========================================
# 💬 6. ฟังก์ชันสร้าง Flex Messages
# ==========================================
def build_order_carousel(orders):
    bubbles = []
    valid_orders = [o for o in orders if o[0] and str(o[0]).strip() != ""]
    
    if not valid_orders: 
        return {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{"type": "text", "text": "📭 ไม่พบรายการใบสั่งขายที่สมบูรณ์", "weight": "bold", "color": "#FF0000", "size": "md"}]}}
        
    for order in valid_orders:
        doc_no, doc_date, net_total, cust_name = str(order[0]), order[1].strftime('%d/%m/%Y') if hasattr(order[1], 'strftime') else str(order[1]), float(order[2]) if order[2] else 0.0, str(order[3]) if order[3] else "ไม่ระบุชื่อลูกค้า"
        bubbles.append({"type": "bubble", "size": "kilo", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{"type": "text", "text": "ใบสั่งขาย (SO)", "weight": "bold", "color": "#1A237E", "size": "sm"}, {"type": "text", "text": doc_no, "weight": "bold", "size": "xl", "margin": "md"}, {"type": "text", "text": f"วันที่: {doc_date}", "size": "xs", "color": "#888888", "margin": "sm"}, {"type": "text", "text": f"ลูกค้า: {cust_name}", "size": "xs", "color": "#1A237E", "margin": "xs", "wrap": True}, {"type": "separator", "margin": "lg"}, {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [{"type": "text", "text": "ยอดรวม", "size": "sm", "color": "#555555"}, {"type": "text", "text": f"฿{net_total:,.2f}", "size": "md", "color": "#2E7D32", "align": "end", "weight": "bold"}]}]}, "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [{"type": "button", "style": "primary", "color": "#2A3A9E", "action": {"type": "message", "label": "ดูรายละเอียด", "text": f"ดูบิล {doc_no}"}}]}})
    return {"type": "carousel", "contents": bubbles[:12]}

def build_po_carousel(pos):
    bubbles = []
    if not pos:
        return {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{"type": "text", "text": "📭 ไม่พบรายการใบสั่งซื้อ (PO)", "weight": "bold", "color": "#FF0000", "size": "md"}]}}
        
    for po in pos:
        cust_po_no, po_date, net_total, internal_doc_no, cust_name = po[0], po[1].strftime('%d/%m/%Y') if hasattr(po[1], 'strftime') else str(po[1]) if po[1] else "-", float(po[2]) if po[2] else 0.0, po[3], po[4]
        bubbles.append({"type": "bubble", "size": "kilo", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{"type": "text", "text": "ใบสั่งซื้อของลูกค้า (PO)", "weight": "bold", "color": "#E65100", "size": "sm"}, {"type": "text", "text": str(cust_po_no), "weight": "bold", "size": "xl", "margin": "md", "wrap": True}, {"type": "text", "text": f"วันที่: {po_date}", "size": "xs", "color": "#888888", "margin": "sm"}, {"type": "text", "text": f"ลูกค้า: {cust_name}", "size": "xs", "color": "#1A237E", "margin": "xs", "wrap": True}, {"type": "separator", "margin": "lg"}, {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [{"type": "text", "text": "ยอดรวม", "size": "sm", "color": "#555555"}, {"type": "text", "text": f"฿{net_total:,.2f}", "size": "md", "color": "#2E7D32", "align": "end", "weight": "bold"}]}]}, "footer": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [{"type": "button", "style": "primary", "color": "#FF6D00", "action": {"type": "message", "label": "ดูรายละเอียด", "text": f"ดูบิล {internal_doc_no}"}}]}})
    return {"type": "carousel", "contents": bubbles[:12]}

def build_price_flex(row):
    doc_date, item_code, item_name, price, cust_name = row[0].strftime('%d/%m/%Y') if hasattr(row[0], 'strftime') else str(row[0]), row[1], row[2], float(row[3]) if row[3] else 0.0, row[4]
    return {"type": "bubble", "size": "kilo", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{"type": "text", "text": "ตรวจสอบราคา", "weight": "bold", "color": "#1A237E", "size": "sm"}, {"type": "text", "text": str(item_code), "size": "xs", "color": "#888888", "margin": "md"}, {"type": "text", "text": item_name, "weight": "bold", "size": "lg", "wrap": True}, {"type": "separator", "margin": "xl"}, {"type": "box", "layout": "horizontal", "margin": "lg", "contents": [{"type": "text", "text": "ราคาล่าสุด", "size": "sm", "color": "#555555"}, {"type": "text", "text": f"฿{price:,.2f}", "size": "xl", "color": "#2E7D32", "align": "end", "weight": "bold"}]}, {"type": "text", "text": f"ขายให้: {cust_name} ({doc_date})", "size": "xxs", "color": "#AAAAAA", "align": "end", "margin": "sm"}]}}

def build_receipt_flex(rows):
    if not rows: 
        return {"type": "bubble", "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": [{"type": "text", "text": "❌ ไม่พบรายละเอียดบิลนี้", "weight": "bold", "color": "#FF0000", "size": "md"}]}}
        
    doc_no, doc_date, total_amnt = rows[0][0], rows[0][1].strftime('%d/%m/%Y') if hasattr(rows[0][1], 'strftime') else str(rows[0][1]), float(rows[0][2]) if rows[0][2] else 0.0
    item_boxes = [{"type": "box", "layout": "vertical", "margin": "md", "contents": [{"type": "text", "text": f"• {row[4] or row[3]}", "size": "sm", "color": "#333333", "wrap": True, "weight": "bold"}, {"type": "box", "layout": "horizontal", "contents": [{"type": "text", "text": f"{(float(row[5]) if row[5] else 0.0):,.0f} x ฿{(float(row[6]) if row[6] else 0.0):,.2f}", "size": "xs", "color": "#888888", "flex": 2}, {"type": "text", "text": f"฿{(float(row[7]) if row[7] else 0.0):,.2f}", "size": "sm", "color": "#2A3A9E", "align": "end", "weight": "bold", "flex": 1}]}]} for row in rows]
    return {"type": "bubble", "size": "mega", "body": {"type": "box", "layout": "vertical", "paddingAll": "24px", "contents": [{"type": "text", "text": "RECEIPT", "weight": "bold", "color": "#1A237E", "size": "sm", "tracking": "md"}, {"type": "text", "text": str(doc_no), "weight": "bold", "size": "xl", "margin": "md"}, {"type": "text", "text": f"วันที่: {doc_date}", "size": "xs", "color": "#888888", "margin": "sm"}, {"type": "separator", "margin": "xl", "color": "#DDDDDD", "thickness": "2px"}, {"type": "box", "layout": "vertical", "margin": "xl", "spacing": "sm", "contents": item_boxes}, {"type": "separator", "margin": "xl", "color": "#DDDDDD", "thickness": "2px"}, {"type": "box", "layout": "horizontal", "margin": "xl", "contents": [{"type": "text", "text": "ยอดรวมทั้งสิ้น", "size": "md", "color": "#555555", "weight": "bold"}, {"type": "text", "text": f"฿{total_amnt:,.2f}", "size": "lg", "color": "#2E7D32", "align": "end", "weight": "bold"}]}]}}

def build_flex(main, lots):
    lot_rows = [{"type": "box", "layout": "horizontal", "backgroundColor": "#F5F5F5" if i % 2 == 0 else "#FFFFFF", "paddingAll": "10px", "contents": [{"type": "text", "text": str(lot[0] if lot[0] else '-'), "size": "sm", "flex": 2, "align": "center", "color": "#666666"}, {"type": "text", "text": f"{int(lot[1]):,}", "size": "sm", "flex": 2, "align": "center", "color": "#333333"}, {"type": "text", "text": "0", "size": "sm", "flex": 2, "align": "center", "color": "#666666"}, {"type": "text", "text": f"{int(lot[3]):,}", "size": "sm", "flex": 2, "align": "center", "color": "#2E7D32" if int(lot[3]) > 0 else "#C62828", "weight": "bold"}]} for i, lot in enumerate(lots)]
    return {"type": "bubble", "header": {"type": "box", "layout": "vertical", "backgroundColor": "#1A237E", "paddingAll": "16px", "contents": [{"type": "box", "layout": "horizontal", "contents": [{"type": "box", "layout": "vertical", "flex": 3, "contents": [{"type": "text", "text": main[0], "color": "#AAAAFF", "size": "xs"}, {"type": "text", "text": main[1] or main[0], "color": "#FFFFFF", "size": "sm", "weight": "bold", "wrap": True}]}]}, {"type": "box", "layout": "horizontal", "backgroundColor": "#2A3A9E", "cornerRadius": "8px", "paddingAll": "12px", "margin": "12px", "contents": [{"type": "box", "layout": "vertical", "flex": 1, "contents": [{"type": "text", "text": "On Hand", "color": "#AAAAFF", "size": "xs", "align": "center"}, {"type": "text", "text": f"{int(main[2]):,}", "color": "#FFFFFF", "size": "xl", "weight": "bold", "align": "center"}]}]}]}, "body": {"type": "box", "layout": "vertical", "paddingAll": "0px", "contents": [{"type": "box", "layout": "horizontal", "backgroundColor": "#E8EAF6", "paddingAll": "10px", "contents": [{"type": "text", "text": "Lot No", "size": "xs", "weight": "bold", "flex": 2, "align": "center"}, {"type": "text", "text": "On Hand", "size": "xs", "weight": "bold", "flex": 2, "align": "center"}, {"type": "text", "text": "จอง", "size": "xs", "weight": "bold", "flex": 2, "align": "center"}, {"type": "text", "text": "พร้อมขาย", "size": "xs", "weight": "bold", "flex": 2, "align": "center"}]}, *lot_rows]}}

def build_summary_bar_flex(rows):
    max_qty = max([float(row[2]) for row in rows]) if rows else 1
    if max_qty == 0: max_qty = 1 
    
    item_rows = []
    for row in rows:
        raw_name = str(row[1] if row[1] else row[0] if row[0] else 'ไม่ระบุชื่อ').strip()
        if not raw_name: raw_name = 'ไม่ระบุชื่อ'
        
        qty = int(row[2])
        percentage = int((qty / max_qty) * 100)
        width_str = f"{percentage}%" if percentage > 0 else "1%"
        
        item_rows.append({
            "type": "box", "layout": "vertical", "margin": "lg",
            "contents": [
                {
                    "type": "box", "layout": "horizontal", "alignItems": "flex-start",
                    "contents": [
                        {"type": "text", "text": "🔸", "size": "xs", "flex": 1, "margin": "sm"},
                        {"type": "text", "text": raw_name, "size": "sm", "color": "#333333", "weight": "bold", "flex": 7, "wrap": True},
                        {"type": "text", "text": f"{qty:,} ชิ้น", "size": "sm", "color": "#1A237E", "weight": "bold", "align": "end", "flex": 3}
                    ]
                },
                {
                    "type": "box", "layout": "vertical", "margin": "md", "backgroundColor": "#E8EAF6", "cornerRadius": "4px", "height": "8px",
                    "contents": [{"type": "box", "layout": "vertical", "backgroundColor": "#2A3A9E", "width": width_str, "height": "8px", "contents": [{"type": "filler"}]}]
                }
            ]
        })
        
    return {
        "type": "bubble", "size": "mega",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "24px",
            "contents": [
                {"type": "text", "text": "📊 กราฟแท่ง (Bar Chart)", "weight": "bold", "color": "#1A237E", "size": "lg", "align": "center"},
                {"type": "text", "text": "ปัดซ้ายเพื่อดูแบบวงกลม 👉", "size": "xs", "color": "#888888", "margin": "sm", "align": "center"},
                {"type": "separator", "margin": "xl"},
                {"type": "box", "layout": "vertical", "contents": item_rows}
            ]
        }
    }

def build_summary_pie_flex(rows):
    import urllib.parse, json
    
    labels = [str(row[1] or row[0] or 'ไม่มีชื่อ').strip() for row in rows]
    data = [int(row[2]) for row in rows]
    
    colors = [
        "#4F46E5", "#F97316", "#22C55E", "#EF4444", "#EAB308", 
        "#0EA5E9", "#8B5CF6", "#EC4899", "#14B8A6", "#F43F5E", 
        "#84CC16", "#64748B"
    ]
    
    chart_config = {
        "type": "pie",
        "data": {
            "labels": labels,
            "datasets": [{
                "data": data,
                "backgroundColor": colors,
                "borderWidth": 2
            }]
        },
        "options": {
            "legend": {"display": False}, 
            "layout": {"padding": 10},
            "plugins": {
                "datalabels": {
                    "color": "#ffffff",
                    "display": "auto", 
                    "font": {"size": 16, "weight": "bold"}
                }
            }
        }
    }
    
    chart_url = f"https://quickchart.io/chart?c={urllib.parse.quote(json.dumps(chart_config))}&w=800&h=800&format=png"
    
    legend_boxes = []
    for i, label in enumerate(labels):
        color = colors[i % len(colors)]
        qty = data[i]
        legend_boxes.append({
            "type": "box", "layout": "horizontal", "margin": "lg", "alignItems": "flex-start",
            "contents": [
                {
                    "type": "box", "layout": "vertical", "width": "12px", "height": "12px", 
                    "cornerRadius": "100px", "backgroundColor": color, "margin": "sm",
                    "contents": [{"type": "filler"}]
                },
                {
                    "type": "text", "text": label, "size": "sm", "color": "#333333", 
                    "weight": "bold", "flex": 7, "wrap": True, "margin": "md"
                },
                {
                    "type": "text", "text": f"{qty:,} ชิ้น", "size": "sm", "color": color, 
                    "weight": "bold", "align": "end", "flex": 3, "margin": "sm"
                }
            ]
        })

    return {
        "type": "bubble", "size": "mega",
        "body": {
            "type": "box", "layout": "vertical", "paddingAll": "24px",
            "contents": [
                {"type": "text", "text": "🎯 สรุปสัดส่วนสต็อก", "weight": "bold", "size": "lg", "color": "#1e293b", "align": "center"},
                {"type": "image", "url": chart_url, "size": "full", "aspectRatio": "1:1", "margin": "lg"},
                {"type": "box", "layout": "vertical", "margin": "md", "contents": legend_boxes},
                {"type": "separator", "margin": "xl"},
                {"type": "text", "text": "👈 ปัดขวาเพื่อดูแบบกราฟแท่ง", "size": "xs", "color": "#94a3b8", "align": "center", "margin": "md"}
            ]
        }
    }

# ==========================================
# ✉️ 6. ฟังก์ชันส่งข้อความ
# ==========================================
def send_text(reply_token, text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(reply_token=reply_token, messages=[TextMessage(text=text)]))

def send_flex(reply_token, flex, alt_text):
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[FlexMessage(alt_text=alt_text, contents=FlexContainer.from_dict(flex))]
            )
        )

# ==========================================
# 🚀 7. Webhook App Routes & Event Handlers
# ==========================================
@app.route('/webhook', methods=['POST'])
def webhook():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try: handler.handle(body, signature)
    except InvalidSignatureError: abort(400)
    return 'OK'

@handler.add(FollowEvent)
def handle_follow(event):
    delete_session(event.source.user_id)
    send_text(event.reply_token, "👋 ยินดีต้อนรับสู่ Hub Bot!\n\nกรุณาล็อกอินก่อนใช้งาน\nพิมพ์: username:password\nเช่น admin:1234")

@handler.add(PostbackEvent)
def handle_postback(event):
    line_user_id = event.source.user_id
    session = get_session(line_user_id)
    
    if session and session.get('selected_server'):
        g.server_key = session['selected_server']

    data = dict(parse_qsl(event.postback.data))
    if data.get('action') == 'select_db':
        db_name = data.get('db_name')
        save_selected_db(line_user_id, db_name)
        send_text(event.reply_token, f"✅ เลือกฐานข้อมูล:\n[{db_name}]\nเรียบร้อยแล้วครับ!\n\nพิมพ์คำสั่ง เช่น 'ใบสั่งขาย', 'ค้นหาสต็อก', 'รายงานสต็อก' ได้เลย")

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    line_user_id = event.source.user_id
    user_text = event.message.text.strip()
    session = get_session(line_user_id)

    if session and session.get('selected_server'):
        g.server_key = session['selected_server']

    if user_text == 'วิธีใช้งาน':
        help_text = (
            "📖 คู่มือการใช้งาน Hub Bot\n"
            "━━━━━━━━━━━━━━\n\n"
            "🛠️ [ การจัดการระบบ ]\n"
            "🔹 'admin:1234' 👉 ล็อกอิน\n"
            "🔹 'เปลี่ยนเซิร์ฟ' 👉 เลือกเซิร์ฟเวอร์ใหม่\n"
            "🔹 'พิมพ์คำสั้นๆ' 👉 ค้นหาและเลือกฐานข้อมูล\n"
            "🔹 'ออกจากระบบ' 👉 ล้างค่าการเชื่อมต่อ\n\n"
            "📦 [ สต็อกและสินค้า ]\n"
            "🔹 'พิมพ์ชื่อ/รหัสสินค้า' 👉 ดูสต็อกและ Lot คงเหลือ\n"
            "🔹 'ราคา <ชื่อสินค้า>' 👉 เช็คราคาขายล่าสุด เช่น 'ราคา ลำไย'\n"
            "🔹 'รายงานสต็อก' 👉 ดูกราฟสรุปสินค้า Top 10\n\n"
            "📄 [ เอกสารและบิล ]\n"
            "🔹 'ใบสั่งขาย' 👉 ดูประวัติ SO ล่าสุด\n"
            "🔹 'ใบสั่งซื้อ' 👉 ดูประวัติ PO ลูกค้าล่าสุด\n"
            "🔹 'ดูบิล <เลขบิล>' 👉 เจาะลึกรายละเอียดบิล"
        )
        send_text(event.reply_token, help_text)
        return

    if user_text == 'admin:1234':
        set_login_session(line_user_id)
        send_flex(event.reply_token, build_server_carousel(), "เลือกเซิร์ฟเวอร์")
        return

    if not session or not session.get('is_logged_in'):
        send_text(event.reply_token, "⚠️ กรุณาล็อกอินก่อนใช้งาน พิมพ์: admin:1234\n💡 พิมพ์ 'วิธีใช้งาน' ดูคำแนะนำ")
        return

    if user_text == 'เปลี่ยนเซิร์ฟ':
        send_flex(event.reply_token, build_server_carousel(), "เลือกเซิร์ฟเวอร์")
        return

    if user_text == 'ออกจากระบบ':
        delete_session(line_user_id)
        send_text(event.reply_token, "👋 ออกจากระบบเรียบร้อยแล้วครับ")
        return

    if user_text.startswith('select_server:'):
        server_key = user_text.split(':')[1]
        save_selected_server(line_user_id, server_key)
        g.server_key = server_key
        
        db_list = get_all_dbs('')
        send_flex(event.reply_token, build_carousel_db_list(db_list, ''), "เลือกฐานข้อมูลแบบสไลด์")
        return

    if not session.get('selected_server'):
        send_flex(event.reply_token, build_server_carousel(), "โปรดเลือกเซิร์ฟเวอร์ก่อนครับ")
        return

    if not session.get('selected_db'):
        db_list = get_all_dbs(user_text)
        if db_list:
            send_flex(event.reply_token, build_carousel_db_list(db_list, user_text), f"ผลค้นหา: {user_text}")
        else:
            db_list_all = get_all_dbs('')
            send_flex(event.reply_token, build_carousel_db_list(db_list_all, ''), "เลือกฐานข้อมูลแบบสไลด์")
        return

    db_name = session['selected_db']
    
    if user_text == 'ค้นหาสต็อก':
        send_text(event.reply_token, f"[{db_name}]\n🔍 กรุณาพิมพ์รหัสหรือชื่อสินค้าที่ต้องการค้นหาครับ")
    elif user_text in ['ใบสั่งขาย (SO)', 'ใบสั่งขาย']:
        orders = get_recent_orders(db_name)
        if not orders: send_text(event.reply_token, f"[{db_name}]\n📭 ยังไม่มีประวัติใบสั่งขายในบริษัทนี้ครับ")
        else: send_flex(event.reply_token, build_order_carousel(orders), "รายการใบสั่งขายล่าสุด")
    elif user_text in ['ใบสั่งซื้อ (PO)', 'ใบสั่งซื้อ']:
        pos = get_recent_customer_pos(db_name)
        if not pos: send_text(event.reply_token, f"[{db_name}]\n📭 ไม่พบประวัติใบสั่งซื้อ PO ของลูกค้าครับ")
        else: send_flex(event.reply_token, build_po_carousel(pos), "รายการใบสั่งซื้อล่าสุด")
    elif user_text.startswith('ดูบิล '):
        target_doc = user_text.replace('ดูบิล ', '').strip()
        detail_rows = get_order_details(target_doc, db_name)
        if not detail_rows: send_text(event.reply_token, f"[{db_name}]\n❌ ไม่พบรายละเอียดของเอกสาร {target_doc} ครับ")
        else: send_flex(event.reply_token, build_receipt_flex(detail_rows), f"ใบเสร็จ {target_doc}")
    elif user_text == 'ราคาสินค้า':
        send_text(event.reply_token, f"[{db_name}]\n💰 เช็คราคาขายล่าสุดของสินค้า\n\nพิมพ์คำว่า 'ราคา' ตามด้วยชื่อหรือรหัสสินค้าได้เลยครับ\n👉 เช่น: ราคา ลำไย, ราคา FG1-00001")
    elif user_text.startswith('ราคา '):
        target_item = user_text.replace('ราคา ', '').strip()
        is_code = bool(re.match(r'^[A-Za-z0-9\-]+$', target_item) and len(target_item) <= 20)
        
        search_results = [target_item] if is_code else ai_search(target_item, db_name)
        search_results = list(dict.fromkeys(search_results))
        
        if not search_results: 
            send_text(event.reply_token, f"[{db_name}]\n❌ ไม่พบสินค้าที่ตรงกับ '{target_item}' ครับ")
        else:
            bubbles = []
            for code in search_results:
                price_row = get_last_price(code, db_name)
                if price_row: bubbles.append(build_price_flex(price_row))
                    
            if bubbles:
                if len(bubbles) == 1: 
                    send_flex(event.reply_token, bubbles[0], f"ราคา {search_results[0]}")
                else: 
                    send_flex(event.reply_token, {"type": "carousel", "contents": bubbles[:10]}, "เปรียบเทียบราคา")
            else: 
                send_text(event.reply_token, f"[{db_name}]\nℹ️ ยังไม่มีประวัติการขายสินค้านี้ในระบบครับ")
    elif user_text in ['รายงานสรุปสต็อก', 'รายงานสต็อก']:
        summary_rows = get_stock_summary(db_name)
        if not summary_rows: 
            send_text(event.reply_token, f"[{db_name}]\n📭 ไม่พบประวัติสต็อกในระบบบริษัทครับ")
        else: 
            bar_bubble = build_summary_bar_flex(summary_rows)
            pie_bubble = build_summary_pie_flex(summary_rows)
            
            carousel_flex = {
                "type": "carousel", 
                "contents": [bar_bubble, pie_bubble]
            }
            send_flex(event.reply_token, carousel_flex, "รายงานสรุปสต็อก")
    else:
        is_code = bool(re.match(r'^[A-Za-z0-9\-]+$', user_text) and len(user_text) <= 20)
        
        search_results = [user_text] if is_code else ai_search(user_text, db_name)
        search_results = list(dict.fromkeys(search_results))
        
        if not search_results:
            send_text(event.reply_token, f"[{db_name}]\n❌ ไม่พบสินค้าที่ตรงกับ '{user_text}' ในบริษัทครับ\n💡 ลองพิมพ์เว้นวรรค เช่น 'ลำไย เล็ก'")
        else:
            bubbles = []
            for code in search_results:
                main, lots = get_stock(code, db_name)
                if main: bubbles.append(build_flex(main, lots))
                    
            if bubbles:
                if len(bubbles) == 1: 
                    send_flex(event.reply_token, bubbles[0], f"สต็อก {search_results[0]}")
                else: 
                    send_flex(event.reply_token, {"type": "carousel", "contents": bubbles[:10]}, "ผลการค้นหาสต็อก")
            else:
                send_text(event.reply_token, f"[{db_name}]\n❌ ไม่พบสินค้านี้ในสต็อกครับ")

init_sqlite_db()

if __name__ == '__main__':
    from pyngrok import ngrok
    ngrok_token = os.environ.get('NGROK_AUTH_TOKEN')
    if ngrok_token:
        ngrok.set_auth_token(ngrok_token)
        
    tunnel = ngrok.connect(5000)
    print(f'Webhook URL: {tunnel.public_url}/webhook')
    app.run(port=5000)