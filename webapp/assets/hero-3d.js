/* Interactive neural-mesh sphere for the hero (Three.js from CDN). */
(function(){
  var el=document.getElementById('hero-orb');
  if(!el){return;}
  if(!window.THREE){el.innerHTML='<img src="assets/brand-emblem.png" alt="" width="110" height="110" style="border-radius:16px">';return;}
  var T=window.THREE;
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var size=el.clientWidth||150;
  var renderer=new T.WebGLRenderer({alpha:true,antialias:true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
  renderer.setSize(size,size);
  el.appendChild(renderer.domElement);
  var scene=new T.Scene();
  var camera=new T.PerspectiveCamera(45,1,0.1,100);
  camera.position.set(0,0,3.2);
  var orb=new T.Group(); scene.add(orb); orb.rotation.x=0.35;
  /* fibonacci sphere points */
  var N=98,pts=[],off=2/N,inc=Math.PI*(3-Math.sqrt(5));
  for(var i=0;i<N;i++){var y=i*off-1+off/2,r=Math.sqrt(1-y*y),phi=i*inc;
    pts.push(new T.Vector3(Math.cos(phi)*r,y,Math.sin(phi)*r));}
  var pg=new T.BufferGeometry().setFromPoints(pts);
  orb.add(new T.Points(pg,new T.PointsMaterial({color:0xC4B5FD,size:0.055,sizeAttenuation:true,transparent:true,opacity:0.95,blending:T.AdditiveBlending,depthWrite:false})));
  var segs=[],th=0.44;
  for(var a=0;a<N;a++){for(var b=a+1;b<N;b++){if(pts[a].distanceTo(pts[b])<th){segs.push(pts[a],pts[b]);}}}
  var lg=new T.BufferGeometry().setFromPoints(segs);
  orb.add(new T.LineSegments(lg,new T.LineBasicMaterial({color:0x8B7BFF,transparent:true,opacity:0.36,blending:T.AdditiveBlending,depthWrite:false})));
  orb.add(new T.Mesh(new T.SphereGeometry(0.55,24,24),new T.MeshBasicMaterial({color:0x6D7BFF,transparent:true,opacity:0.10,blending:T.AdditiveBlending,depthWrite:false})));
  /* pointer-drag rotation + momentum */
  var dragging=false,lx=0,ly=0,vx=0,vy=0;
  el.addEventListener('pointerdown',function(e){dragging=true;el.classList.add('dragging');lx=e.clientX;ly=e.clientY;vx=vy=0;el.setPointerCapture&&el.setPointerCapture(e.pointerId);});
  window.addEventListener('pointermove',function(e){if(!dragging)return;var dx=e.clientX-lx,dy=e.clientY-ly;lx=e.clientX;ly=e.clientY;vy=dx*0.006;vx=dy*0.006;orb.rotation.y+=vy;orb.rotation.x+=vx;});
  window.addEventListener('pointerup',function(){dragging=false;el.classList.remove('dragging');});
  window.addEventListener('resize',function(){var s=el.clientWidth||150;renderer.setSize(s,s);});
  (function tick(){requestAnimationFrame(tick);
    if(!dragging){if(!reduce){orb.rotation.y+=0.0032;}orb.rotation.y+=vy;orb.rotation.x+=vx;vy*=0.94;vx*=0.94;}
    renderer.render(scene,camera);})();
})();
