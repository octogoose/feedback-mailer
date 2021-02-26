from aiohttp import web, web_request, ClientSession, FormData
from asyncio import create_task, gather
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from os import environ
from collections import OrderedDict
from uuid import uuid1
from limited_dict import LimitedSizeDict
import aiohttp
import aiosmtplib

defaults = {
    'subject': '',
    'text': '',
    'max_file_size': 10 * 1024 * 1024, # 10 MB
}

class Mailer:
    def __init__(self):
        self.files = LimitedSizeDict()
        self.username = environ.get('MAIL_USERNAME', None)
        self.password = environ.get('MAIL_PASSWORD', None)
        self.recepient = environ.get('RECEPIENT', None)
        
        if not self.recepient and self.username and self.password:
            self.recepient = self.username
                
        self.hostname = environ.get('SMTP_HOSTNAME', None)
        self.port = int(environ.get('SMTP_PORT', 0))
        
        self.slack_token = environ.get('SLACK_TOKEN', None)
        self.slack_channel = environ.get('SLACK_CHANNEL', None)
        
        self.max_file_size = int(environ.get('MAX_FILE_SIZE', defaults['max_file_size']))
        
        self.app = web.Application(client_max_size=self.max_file_size)
        
        if self.slack_token and self.slack_channel:
            self.app.add_routes([web.post('/slack', self.slack_send_handler)])
        elif not self.username or not self.password or not self.hostname:
            exit('Incorrect configuration')
        
        if self.hostname and self.username and self.password:
            self.app.add_routes([web.post('/mail', self.send_handler)])
            
        self.app.add_routes([web.post('/upload', self.file_upload_handler)])
        
        self.mail_kwargs = {
            'hostname': self.hostname,
            'username': self.username,
            'password': self.password,
        }
        
        if self.port:
            self.mail_kwargs['port'] = self.port
        
        smtp_tls = environ.get('SMTP_TLS', None)
        
        if smtp_tls == 'plain':
            pass
        elif smtp_tls == 'starttls':
            self.mail_kwargs['start_tls'] = True
        else:
            self.mail_kwargs['use_tls'] = True


    async def send_handler(self, request):
        json = await request.json()
        
        message = MIMEMultipart()
        message['From'] = self.username
        message['To'] = self.recepient
        subject = json.get('subject', defaults['subject'])
        user_id = json.get('user_id', '')
        
        if not user_id:
            return web.HTTPBadRequest(text=f'{{"error": "user_id required"}}')
            
        subject = f'[{user_id}] {subject}'
        message['Subject'] = subject
        message.attach(MIMEText(json.get('text', defaults['text'])))

        for file in json.get('files', []):
            if file in self.files:
                filename, file_content = self.files.pop(file)
                message_part = MIMEApplication(file_content, Name=filename)
                message_part['Content-Disposition'] = f'attachment; filename="{filename}"'
                message.attach(message_part)
                
        res = create_task(aiosmtplib.send(message, **self.mail_kwargs))
        
        try:
            return web.json_response({'response': (await res)[1]})
        except aiosmtplib.SMTPResponseException as e:
            return web.HTTPBadRequest(text=f'{{"code": {e.code}, "message": "{e.message}"}}')
        except aiosmtplib.SMTPException as e:
            return web.HTTPBadRequest(text=f'{{"error": "{e.message}"}}')


    async def file_upload_handler(self, request):
        data = await request.post()
        file = data.get('file', None)
        
        if not file:
            return web.HTTPBadRequest(text='{"error": "No file received"}')
        if not isinstance(file, web_request.FileField):
            return web.HTTPUnprocessableEntity(text='{"error": "Unprocessable file received"}')
            
        filename = file.filename
        file_content = file.file.read()
        random_name = str(uuid1())
        
        self.files[random_name] = (filename, file_content)
        
        return web.json_response({'response': random_name})
        
        
    async def upload_file_worker(self, file, ts):
        filename, content = self.files.pop(file)
        async with ClientSession() as session:
            form = FormData()
            form.add_field('file', content, filename=filename)
            
            response = await session.post('https://slack.com/api/files.upload', data=form,
                                params={
                                    'channels': self.slack_channel,
                                    'thread_ts': ts,
                                }, headers={
                                    'Authorization': f'Bearer {self.slack_token}',
                                })
                                
            response = await response.json()
            print(response)
            if not response['ok']:
                return response
        
            return {'ok': True}
        
        
    async def slack_files_upload(self, files, ts):
        uploaded = []
        files = [file for file in files if file in self.files]
        for file in files:
            uploaded.append(create_task(self.upload_file_worker(file, ts)))
        return await gather(*uploaded)
                
        
    async def slack_chat_post_message(self, text):
        url = 'https://slack.com/api/chat.postMessage'
        async with ClientSession() as session:
            response = await session.post(url, data={
                                'text': text,
                                'channel': self.slack_channel,
                                'mrkdwn': False,
                              }, headers={
                                  'Authorization': f'Bearer {self.slack_token}',
                              })
            response = await response.json()
            print(response)
            return response['ts']
    
    
    async def slack_send_handler(self, request):
        json = await request.json()
        subject = json.get('subject', defaults['subject'])
        text = json.get('text', defaults['text'])
        user_id = json.get('user_id', '')
        
        if not user_id:
            return web.HTTPBadRequest(text=f'{{"error": "user_id required"}}')
            
        subject = f'[{user_id}] {subject}'
        
        files = json.get('files', [])
        
        if not subject and not text and not files:
            return web.HTTPBadRequest(text='{"error": "fields subject, text or files required"}')
        
        message = '\n\n'.join((subject, text))
        
        ts = await self.slack_chat_post_message(message)
        
        if files:
            return web.json_response(await self.slack_files_upload(files, ts))
        
        return web.json_response({'ok': True})


if __name__ == '__main__':
    mailer = Mailer()
    web.run_app(mailer.app)
