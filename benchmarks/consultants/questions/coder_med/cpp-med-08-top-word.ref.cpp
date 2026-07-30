#include <iostream>
#include <string>
#include <unordered_map>
int main(){
    std::unordered_map<std::string,long> c;
    std::string w;
    while(std::cin >> w) c[w]++;
    std::string best; long bc=-1;
    for(auto &kv : c){
        if(kv.second>bc || (kv.second==bc && kv.first<best)){
            bc=kv.second; best=kv.first;
        }
    }
    std::cout << best << "\n";
    return 0;
}
