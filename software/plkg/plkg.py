import datastream.csi_interface as csi_interface
import datastream.load as load
import plkg.greycode_quantization as quan
import plkg.ecc as ecc
import time
import plkg.sha256 as sha256
class end_device:
    def __init__(self,device_tag):
        self.device_tag = device_tag
        # self.esp0 = csi_interface.com_esp('/dev/ttyUSB0',921600)#7setting up device
        if device_tag == 'U':
            self.magic = True
            self.esp0 = csi_interface.com_esp('/dev/ttyUSB0',921600)
            self.filename = 'filenameU'
        elif device_tag == 'I':
            self.magic = False
            self.esp0 = csi_interface.com_esp('/dev/ttyUSB2',921600)
            self.filename = 'filenameI'
        
        self.reconciliation_result = ''
        #csi average
        self.csi_average = ''

        #plkg parameter
        self.quantization_result = ''
        self.key = b''

        #data exchange system
        self.chatmanager = False

        #save parameter
        self.save = False
        

    def set_chatmanager(self,chatmanager):
        self.chatmanager = chatmanager
        
    def save_probing_result(self,filename):
        self.save = True
        self.filename = filename

    def time_synchronize(self):
        if not self.chatmanager:
            print("Error: need to assign chatmanager")
            return False
            
        # Очищаем очередь перед началом, чтобы удалить старые сообщения
        self.chatmanager.queue_clear()

        # === ИНИЦИАТОР (Тот, кто начинает) ===
        if self.magic:
            print("1. [Sync] Отправляю запрос...")
            ack = 'FAIL'
            
            # Шлем -check, пока не получим ответ
            while ack != '-check':
                self.chatmanager.send_line('-check')
                
                # Ждем ответа 0.5 секунды, проверяя очередь
                # Вместо time.sleep(0.5) делаем цикл, чтобы не тупить, если ответ пришел быстро
                start_time = time.time()
                while time.time() - start_time < 0.5:
                    msg = self.chatmanager.pop_line()
                    if msg == '-check':
                        ack = '-check'
                        break
                    time.sleep(0.01) # Не грузим процессор

            print("2. [Sync] Ответ получен. Отправляю сигнал старта...")
            self.chatmanager.send_line('-bang')
            return True

        # === ПОЛУЧАТЕЛЬ (Тот, кто ждет) ===
        elif not self.magic:
            print("1. [Sync] Жду собеседника...")
            
            while True:
                msg = self.chatmanager.pop_line()
                
                if msg == '-check':
                    # ВАЖНО: Если нас спрашивают - отвечаем.
                    # Даже если спрашивают 10 раз подряд (значит инициатор не услышал нас)
                    self.chatmanager.send_line('-check')
                    
                elif msg == '-bang':
                    # Ура, инициатор нас услышал и дал команду старта
                    print("2. [Sync] Старт получен!")
                    # Очищаем очередь, чтобы начать чистый чат
                    self.chatmanager.queue_clear() 
                    return True
                
                else:
                    # Если пусто или мусор - спим чуть-чуть
                    time.sleep(0.01)


    def channel_probing(self):
        self.esp0.run_collection(self.magic,1,10)#manage the order pf probing
        if self.save:
            csi_interface.savetocsv(self.filename,self.esp0.aquire_csi())
        self.save = False
        
    def quantization(self):
        csi_data = self.esp0.aquire_csi()
        self.csi_average = quan.average(load.transform(csi_data))
        self.quantization_result = quan.quantization_1(self.csi_average,2,13)
    
    def information_reconciliation(self):
        if self.magic:
            time.sleep(0.5)
            ecc_code = ecc.reconciliation_encode(self.quantization_result)
            # self.chatmanager.send_line(ecc_code)
            print("Sending ECC code:", ecc_code)
            self.reconciliation_result = self.quantization_result
        elif not self.magic:
            reconcilation_result = input()
            self.reconciliation_result = ecc.reconciliation_decode(self.quantization_result,reconcilation_result)
    
    def privacy_amplification(self):
        self.key = sha256.sha_byte(self.quantization_result)

    def plkg(self):
        # if self.time_synchronize():
        self.channel_probing()
        self.quantization()
        self.information_reconciliation()
        self.privacy_amplification()
        self.chatmanager.queue_clear()
        # else:
            # return b"FAIL"
        return self.key
