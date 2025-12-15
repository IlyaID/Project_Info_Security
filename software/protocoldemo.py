from plkg import plkg
from datastream import chat

c = chat.chat_manager("200.1.1.1", 5000, 5001)#need to input ip of enddevice
c.chat_init()
x = plkg.end_device('U')

x.set_chatmanager(c)
print(x.plkg())