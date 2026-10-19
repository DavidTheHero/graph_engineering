const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
(async()=>{
 const code=fs.readFileSync(path.resolve(__dirname,'../../src/graph_engineering/web/centre-motion.js'),'utf8');
 const {quadraticPoint}=await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'));
 const a={x:0,y:0},c={x:10,y:20},b={x:20,y:0};
 assert.deepEqual(quadraticPoint(a,c,b,0),a);
 assert.deepEqual(quadraticPoint(a,c,b,1),b);
 assert.deepEqual(quadraticPoint(a,c,b,.5),{x:10,y:10});
 assert.deepEqual(quadraticPoint(a,c,b,-1),a);
 assert.deepEqual(quadraticPoint(a,c,b,2),b);
 for(const value of [NaN,Infinity,'0.5',null])assert.throws(()=>quadraticPoint(a,c,b,value),TypeError);
 assert.throws(()=>quadraticPoint({x:NaN,y:0},c,b,.5),TypeError);
 console.log('Bezier marker motion: 10 boundary/curve checks passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
