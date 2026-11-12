# نقل Novelty Lens إلى أرامكو بصورة Docker واحدة

الحزمة تحتوي **صورة واحدة** لتشغيل Angular وFastAPI وعامل البحث المتقدم
وPostgreSQL 17 وQdrant 1.18.3 وNginx. البرامج ومكتباتها داخل الصورة؛ لا تحتاج
لتنزيل Python أوNode أوصور إضافية على الجهاز الثاني.

يجب أن يتوفر Docker يعمل بوضع **Linux containers** على جهاز أرامكو
(معمارية x64 / amd64). ملف ZIP وحده لا يغني عن وجود محرك Docker.
البحث يحتاج وصولًا فعليًا من الحاوية إلى MetaBrain وLens وخدمة embeddings
المحددة في الإعدادات. نقل الصورة لا يمنح الوصول لهذه الخدمات.

## على الجهاز الشخصي: بناء الحزمة

من مجلد المشروع في PowerShell:

```powershell
.\docker-single\Build-Transfer.ps1
```

الناتج `.artifacts\NoveltyLens-Aramco.zip` ومعه ملف SHA256 للتحقق من النقل.
البناء يحتاج الإنترنت. الحزمة لا تضم المفاتيح ولا `backend/.env` ولا قواعد
البيانات الحالية ولا سجل البحث؛ تبدأ بقاعدة جديدة على الجهاز الثاني.
إذا كانت شبكة البناء تحتاج شهادة CA خاصة، مرر ملف PEM المعتمد للبناء فقط:

```powershell
.\docker-single\Build-Transfer.ps1 -BuildCaFile C:\Certificates\build-ca.pem
```

شهادة البناء لا تدخل إعدادات التطبيق؛ شهادة جهاز أرامكو تمرر عند التشغيل.
يمكن تسريع البناء باستخدام `-DependencyImage agenticai-main-backend:latest`
إذا كانت صورة backend موجودة محليًا وتستخدم Python 3.13 على Debian trixie.
يُنسخ منها مجلد مكتبات Python فقط؛ يظل كود التطبيق من المشروع الحالي، ويتحقق
pip من استيفاء `backend/requirements.txt`.

## على جهاز أرامكو

1. انقل ZIP وفك الضغط إلى مجلد محلي.
2. افتح PowerShell في هذا المجلد، وانسخ ملف الإعدادات:

```powershell
Copy-Item runtime.env.example runtime.env
notepad runtime.env
```

3. أدخل مفاتيح Lens وMetaBrain وembeddings، واضبط عنوان ونموذج وصيغة
   embeddings الفعلية المتاحة لك. الإعداد المبدئي يصف BGE الحالي؛ إذا كانت
   خدمة أرامكو تستخدم نموذجًا آخر فاكتب اسمه وصيغته وعنوانه معًا.
   القيم في `runtime.env` حرفية: بدون علامات اقتباس وبدون `${VARIABLE}`.
4. شغّل:

```powershell
.\Start-Aramco.ps1
```

إذا كانت سياسة PowerShell تمنع تشغيل السكربتات، نفذ أوامر Docker مباشرة
بدون تغيير سياسة الجهاز (بعد تعبئة `runtime.env`):

```powershell
docker load --input novelty-lens-image.tar
docker run --detach --pull=never --name novelty-lens --stop-timeout 90 --env-file runtime.env --publish 127.0.0.1:4200:8080 --mount type=volume,source=novelty-lens-data,target=/data novelty-lens:aramco
docker inspect novelty-lens --format '{{.State.Health.Status}}'
```

انتظر حتى تكون الحالة `healthy`. سكربت التشغيل ينفذ كذلك التحقق من checksum
والانتظار تلقائيًا.

5. عندما تظهر `Ready` افتح **http://localhost:4200**.
   حساب المشروع المبدئي: `testnovelty@gmail.com` وكلمة المرور `ABC123`.
   المنفذ منشور على الجهاز المحلي فقط.

السكربت يتحقق من SHA256، ثم يستخدم `docker load` و`docker run --pull=never`.
لا يحتاج registry أو Compose أوإنترنت لتحميل البرامج وتشغيلها؛ طلبات البحث
نفسها تتصل بالخدمات المحددة. يُنشئ المخطط وجداول checkpoints تلقائيًا قبل
تشغيل API والعامل. إن فشلت خدمة، تتوقف الحاوية بخطأ واضح.

إذا احتجت منفذًا مختلفًا:

```powershell
.\Start-Aramco.ps1 -Port 4300
```

إذا احتاج اتصال الشركة شهادة CA، مرر ملف PEM المعتمد؛ لا تعطّل TLS:

```powershell
.\Start-Aramco.ps1 -CaFile C:\Certificates\company-ca.pem
```

يمكن إضافة `HTTPS_PROXY` و`HTTP_PROXY` إلى `runtime.env` عند الحاجة، مع إبقاء
`NO_PROXY=localhost,127.0.0.1` حتى تبقى الاتصالات الداخلية مباشرة.

## الإيقاف والتشغيل والبيانات

```powershell
docker stop -t 90 novelty-lens
docker start novelty-lens
docker logs --tail 50 novelty-lens
docker inspect novelty-lens --format '{{.State.Health.Status}}'
```

البيانات في volume باسم `novelty-lens-data`، منفصل عن الصورة؛ يبقى بعد إيقاف
أوحذف الحاوية. لا تحذف هذا الـvolume إذا أردت الاحتفاظ بالتاريخ.
لتطبيق تغيير في `runtime.env`، أوقف واحذف الحاوية فقط ثم أعد تشغيل السكربت:

```powershell
docker stop -t 90 novelty-lens
docker rm novelty-lens
.\Start-Aramco.ps1
```

صورة Docker المحفوظة ليست نسخة احتياطية للـvolume. انسخ بيانات التشغيل
احتياطيًا بشكل منفصل إذا احتجت نقل التاريخ لاحقًا.

مرجع طريقة حفظ/تحميل الصور: [Docker save](https://docs.docker.com/reference/cli/docker/image/save/)
و[Docker load](https://docs.docker.com/reference/cli/docker/image/load/).
