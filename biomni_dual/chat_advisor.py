"""Optional, read-only OpenAI-compatible chat advisor for the Control Center.

It only returns text. No tools, filesystem, task writes, or execution authority.
Credentials live server-side and are never returned in the browser/API.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class OpenAICompatibleAdvisor:
    def __init__(self, base_url: str, api_key: str, model: str, *, timeout: float = 25):
        url=base_url.rstrip('/')
        parts=urlsplit(url)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('Invalid model endpoint')
        if parts.scheme != 'https' and not (parts.scheme=='http' and parts.hostname in ('127.0.0.1','localhost','::1')):
            raise ValueError('Model endpoint requires HTTPS or loopback HTTP')
        if not api_key or not model or len(api_key)>10000 or not 0<timeout<=120:
            raise ValueError('Missing model credentials/configuration')
        self.endpoint=url+'/chat/completions'
        self.api_key=api_key
        self.model=model
        self.timeout=timeout

    def __call__(self, message: str, history: tuple[dict,...]) -> str:
        context=[{'role':'system','content':
            'You are an advisory assistant inside Biomni Control Center. '
            'Answer in the language of the user. You may suggest task plans, but do not '
            'claim to have created tasks, executed agents, approved changes, or inspected '
            'tools/data that you did not actually receive. You have no tools or execution privileges.'}]
        for item in history[-20:]:
            if item.get('role') in ('user','assistant') and isinstance(item.get('text'),str):
                context.append({'role':item['role'],'content':item['text'][:3000]})
        # Include the current user turn even when caller has not yet saved it.
        if not context or context[-1].get('role') != 'user' or context[-1].get('content') != message[:3000]:
            context.append({'role':'user','content':message[:3000]})
        payload=json.dumps({'model':self.model,'messages':context,'stream':False,
                            'max_tokens':750},ensure_ascii=False).encode()
        req=Request(self.endpoint,data=payload,headers={
            'Content-Type':'application/json', 'Authorization':'Bearer '+self.api_key,
            'Accept':'application/json'},method='POST')
        with urlopen(req,timeout=self.timeout) as response:
            if int(response.headers.get('Content-Length','0'))>128000:
                raise ValueError('Model response too large')
            output=json.loads(response.read(128001))
        answer=output['choices'][0]['message']['content']
        if not isinstance(answer,str) or not answer.strip():
            raise ValueError('Model returned no text')
        return answer[:4000]
