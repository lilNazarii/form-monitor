import os
bind='0.0.0.0:'+os.environ.get('PORT','10000')
workers=1
worker_class='gthread'
threads=8
timeout=60
graceful_timeout=540
accesslog=None  # OAuth callback query strings contain authorization codes.
errorlog='-'
capture_output=True
preload_app=False
