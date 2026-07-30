#include <iostream>
#include <vector>
#include <algorithm>
int main() {
    long long x;
    std::vector<long long> v;
    while (std::cin >> x) v.push_back(x);
    std::sort(v.begin(), v.end());
    for (size_t i = 0; i < v.size(); i++) {
        if (i) std::cout << ' ';
        std::cout << v[i];
    }
    std::cout << std::endl;
    return 0;
}
