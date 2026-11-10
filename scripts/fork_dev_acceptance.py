"""Disposable fork UI acceptance: real API/store/file tools, deterministic model only.

Run --home <disposable directory> --port 19787. Vite uses
VITE_RYN_NODE_BASE_URL=http://127.0.0.1:19787/api/local, /agent?client=fixture.
Only node-shell data and model responses are fixtures. No paid services.
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from test_agent_file_changes import environment
from test_agent_task_forks import FileTools
from test_agent_runtime import assistant

class FixedModel:
    def __init__(self,base): self.base=base
    def complete(self,model_id,messages,tool_specs,cancelled,on_event):
        last=max(i for i,m in enumerate(messages) if m.get('role')=='user')
        prompt=messages[last]['content']
        label='child' if 'child' in prompt.lower() else 'parent'
        writes=[m for m in messages[last+1:] if m.get('role')=='tool']
        with (self.base/'model-calls.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'prompt':prompt,'label':label,'tool_results':len(writes)},ensure_ascii=False)+'\n')
        if len(writes)==0:
            return assistant(name='ryn_file_write',args={'path':'shared.txt','content':label+' result\n'})
        if len(writes)==1:
            return assistant(name='ryn_file_write',args={'path':label+'-only.txt','content':label+' independent artifact\n'})
        return assistant(label.upper()+' independent result. Saved shared.txt and '+label+'-only.txt. Original conversation remains unchanged.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--home',required=True);parser.add_argument('--port',type=int,default=19787);args=parser.parse_args()
    base=Path(args.home).resolve()
    app,runtime,local,workspace,_=environment(base,guard=False)
    runtime.tools=FileTools(local);runtime.model=FixedModel(base)
    app.add_middleware(CORSMiddleware,allow_origins=['http://127.0.0.1:15173','http://localhost:15173'],allow_credentials=True,allow_methods=['GET','POST','PUT','DELETE'],allow_headers=['Content-Type'])
    state_file=base/'acceptance.json'
    if not state_file.exists():
        (workspace/'shared.txt').write_text('original content\n',encoding='utf-8')
        row=runtime.create({'message':'Create the parent approach and preserve this original task.','title':'Fork acceptance — original task'})
        for _ in range(12):
            runtime.run_once()
            if runtime.get(row['id'])['status'] in {'completed','failed'}:break
        current=runtime.get(row['id'])
        assert current['status']=='completed',current
        state_file.write_text(json.dumps({'parent_id':row['id'],'workspace':str(workspace)}),encoding='utf-8')
        (base/'parent-before.json').write_text(json.dumps(runtime.store.read()['tasks'][row['id']],ensure_ascii=False,indent=2),encoding='utf-8')
    stop=threading.Event()
    def worker():
        while not stop.is_set():
            runtime.run_once();stop.wait(.1)
    thread=threading.Thread(target=worker,daemon=True);thread.start()
    print(state_file.read_text(),flush=True)
    try:uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
    finally:stop.set();thread.join(timeout=5);local.processes.close()
if __name__=='__main__':main()
