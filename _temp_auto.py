import time
import builtins
import sys

def auto_input(prompt=''):
    print(prompt, flush=True)
    print("\n[Auto] I am now waiting 60 seconds for you to authorize in the browser...", flush=True)
    time.sleep(60)
    return ""

builtins.input = auto_input

import generate_oauth2_token
generate_oauth2_token.main()
