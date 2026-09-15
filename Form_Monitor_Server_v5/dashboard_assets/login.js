'use strict';
const $=id=>document.getElementById(id);
let csrf='', registering=false, setup=false;
function mode(){
 $('title').textContent=setup?'Create your owner account':registering?'Create an account':'Welcome back.';
 $('intro').textContent=setup?'Set up the owner account for this server.':registering?'Start with a workspace that belongs to you.':'Your forms, checks, and connections. Right where you left them.';
 $('registration').hidden=!registering;$('owner-setup').hidden=!setup;$('setup-code').required=setup;
 $('confirmation').required=registering;$('password').minLength=registering?12:1;
 $('password').autocomplete=registering?'new-password':'current-password';
 $('submit').textContent=registering?'Create account':'Sign in';
 $('switch').textContent=registering?'Already have an account? Sign in':'New here? Create an account';$('switch').hidden=setup;$('error').textContent='';
}
$('switch').onclick=()=>{registering=!registering;mode();};
$('auth-form').onsubmit=async e=>{e.preventDefault();$('error').textContent='';
 if(registering&&$('password').value!==$('confirmation').value){$('error').textContent='Passwords do not match.';return;}
 $('submit').disabled=true;
 try{const r=await fetch('/api/'+(registering?'register':'login'),{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({email:$('email').value,password:$('password').value,setup_code:$('setup-code').value.trim()})});const data=await r.json();if(!r.ok)throw Error(data.error||'Could not sign in.');location.replace('/workspace'+(location.hash||'#monitors'));}
 catch(e){$('error').textContent=e.message;}finally{$('submit').disabled=false;}
};
(async()=>{try{const r=await fetch('/api/session');if(!r.ok)throw Error('Could not connect. Refresh the page.');const data=await r.json();if(data.user){location.replace('/workspace'+(location.hash||'#monitors'));return;}csrf=data.csrf;setup=data.setup_required;registering=setup;mode();}catch(e){$('error').textContent=e.message;$('submit').disabled=true;}})();


$('toggle-password').onclick=()=>{const show=$('password').type==='password';$('password').type=show?'text':'password';$('toggle-password').textContent=show?'Hide':'Show';$('toggle-password').setAttribute('aria-label',show?'Hide password':'Show password');$('toggle-password').setAttribute('aria-pressed',String(show));};
