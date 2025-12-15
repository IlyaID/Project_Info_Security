from plkg import plkg
from datastream import chat

c = chat.chat_manager("200.1.1.1", 5001, 5000)#need to input ip of enddevice
c.chat_init()
x = plkg.end_device('I')
x.set_chatmanager(c)
print(x.plkg())