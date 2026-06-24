# 🌟 เปลี่ยนมาใช้ Linux Debian 11 (Bullseye) ที่ยังรองรับการอัปเดต
FROM python:3.10-slim-bullseye

# ติดตั้งตัวช่วยโหลดไฟล์และ Driver พื้นฐาน
RUN apt-get update && apt-get install -y curl gnupg g++ unixodbc-dev

# 🌟 เปลี่ยนลิงก์โหลด Microsoft ODBC Driver 17 ให้ตรงกับ Debian 11
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
RUN curl https://packages.microsoft.com/config/debian/11/prod.list > /etc/apt/sources.list.d/mssql-release.list
RUN apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql17

# ตั้งค่าโฟลเดอร์ทำงาน
WORKDIR /app

# ก๊อปปี้ไฟล์และติดตั้งไลบรารีของ Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ก๊อปปี้โค้ดทั้งหมดของเราลงไป
COPY . .

# สั่งรันแอปพลิเคชันผ่าน Gunicorn
# เพิ่ม timeout เป็น 60 วินาที เพื่อให้บอทมีเวลาตื่นนานขึ้น
CMD gunicorn --bind 0.0.0.0:$PORT app:app --timeout 60