from django.core.mail.backends.smtp import EmailBackend
from django.conf import settings
import os
import ssl
import smtplib


class CustomEmailBackend(EmailBackend):
    def open(self):
        """打开SMTP连接，使用自定义SSL上下文"""
        if self.connection:
            return False

        # H10 修复：默认使用受信 CA 校验证书；仅对确需自签证书的内部 SMTP，
        # 设置环境变量 EMAIL_TLS_VERIFY=0 才放宽校验。严禁默认关闭校验。
        if os.environ.get("EMAIL_TLS_VERIFY", "1") == "0":
            ssl_context = ssl._create_unverified_context()
        else:
            ssl_context = ssl.create_default_context()
        
        try:
            if self.use_ssl:
                # 使用SSL连接 (端口465)
                self.connection = smtplib.SMTP_SSL(
                    self.host, 
                    self.port, 
                    context=ssl_context,
                    timeout=self.timeout
                )
            else:
                # 使用普通连接，然后升级到TLS (端口587)
                self.connection = smtplib.SMTP(
                    self.host, 
                    self.port, 
                    timeout=self.timeout
                )
                if self.use_tls:
                    self.connection.starttls(context=ssl_context)
            
            if self.username and self.password:
                self.connection.login(self.username, self.password)
            return True
        except Exception:
            if not self.fail_silently:
                raise