set filepath=%~dp0
call %filepath%venv\Scripts\activate.bat
python %filepath%clean_my_messages.py
call %filepath%venv\Scripts\deactivate.bat
