# ใช้ Linux Debian เวอร์ชันที่มี Python 3.10
FROM python:3.10-slim-buster

# ติดตั้งตัวช่วยโหลดไฟล์และ Driver พื้นฐาน
RUN apt-get update && apt-get install -y curl gnupg g++ unixodbc-dev

# ติดตั้ง Microsoft ODBC Driver 17 for SQL Server
RUN curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add -
RUN curl https://packages.microsoft.com/config/debian/10/prod.list > /etc/apt/sources.list.d/mssql-release.list
RUN apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql17

# ตั้งค่าโฟลเดอร์ทำงาน
WORKDIR /app

# ก๊อปปี้ไฟล์และติดตั้งไลบรารีของ Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ก๊อปปี้โค้ดทั้งหมดของเราลงไป
COPY . .

# สั่งรันแอปพลิเคชันผ่าน Gunicorn (สำหรับ Production)
CMD gunicorn app:app -b 0.0.0.0:$PORT