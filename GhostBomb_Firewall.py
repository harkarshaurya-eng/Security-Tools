#This is only made to understand how does a firewall works, using blocking IPs firewall rules

import random #to generate random ip addresses


#access the key and value in the dictionary firewall_rules to return allow or block
def check_firewall_rules(ip,rules):
    for rule_ip, action in rules.items():
        if ip == rule_ip:
            return action
        return "allow"

def main():
	firewall_rules = {
            "192.168.1.1": "block",
            "192.168.1.3": "block",
            "192.168.1.23":"block",
            "192.168.1.7":"block",
            "192.168.1.19":"block",
            "192.168.1.12":"block"
            }	
    
    for _ in range(1, 12):
        ip_adress = f"192.168.1{random.randint(0,25)}"
        action = check_firewall_rules(ip_address, firewall_rules)
        random_number = random.randint(0,9999)
        print(f"IP:{ip_address}, Action:{action},Random:{random_number}")

	
if __name__ == "__main__":
    main()
